# ============================================================
# scanner.py — Python pre-filter (zero API cost)
# Runs 9 checks. Must score >= 6 to pass to Claude.
# Prevents wasting Claude API calls on bad setups.
# ============================================================

import logging
from config import CONFIG

logger = logging.getLogger(__name__)


def run_prefilter(pair, ind, spread_pips):
    """
    Run all 9 pre-filter checks on latest indicators.

    Args:
        pair:        e.g. "EUR_USD"
        ind:         dict from indicators.get_latest()
        spread_pips: current live spread in pips (float or None)

    Returns:
        passed (bool), score (int), details (dict), bias (str)
    """
    if ind is None:
        return False, 0, {"error": "No indicator data"}, "NEUTRAL"

    details = {}
    score   = 0

    # --------------------------------------------------------
    # CHECK 1: Spread acceptable
    # --------------------------------------------------------
    max_spread = CONFIG["max_spread_pips"].get(pair, 2.0)
    if spread_pips is None:
        details["spread"] = {"pass": True, "value": "unknown", "reason": "spread unavailable — benefit of doubt"}
        score += 1
    elif spread_pips <= max_spread:
        details["spread"] = {"pass": True, "value": spread_pips, "reason": f"{spread_pips} pips OK"}
        score += 1
    else:
        details["spread"] = {"pass": False, "value": spread_pips, "reason": f"{spread_pips} pips too wide (max {max_spread})"}

    # --------------------------------------------------------
    # CHECK 2: ATR in valid range
    # --------------------------------------------------------
    min_atr = CONFIG["min_atr"].get(pair, 0.0005)
    max_atr = CONFIG["max_atr"].get(pair, 0.003)
    atr     = ind["atr"]

    if min_atr <= atr <= max_atr:
        details["atr"] = {"pass": True, "value": round(atr, 6), "reason": "volatility in range"}
        score += 1
    elif atr < min_atr:
        details["atr"] = {"pass": False, "value": round(atr, 6), "reason": "market too quiet"}
    else:
        details["atr"] = {"pass": False, "value": round(atr, 6), "reason": "market too volatile (possible news)"}

    # --------------------------------------------------------
    # CHECK 3: RSI not in dead zone (40-60, tightened from 38-62)
    # --------------------------------------------------------
    rsi = ind["rsi"]
    if rsi < 40 or rsi > 60:
        details["rsi_zone"] = {"pass": True, "value": round(rsi, 1), "reason": f"RSI {rsi:.1f} shows conviction"}
        score += 1
    else:
        details["rsi_zone"] = {"pass": False, "value": round(rsi, 1), "reason": f"RSI {rsi:.1f} dead zone (40-60)"}

    # --------------------------------------------------------
    # CHECK 4: RSI not extreme (> 78 or < 22 = exhaustion, tightened)
    # --------------------------------------------------------
    if 22 < rsi < 78:
        details["rsi_extreme"] = {"pass": True, "value": round(rsi, 1), "reason": "RSI not exhausted"}
        score += 1
    else:
        details["rsi_extreme"] = {"pass": False, "value": round(rsi, 1), "reason": f"RSI {rsi:.1f} extreme — exhaustion risk"}

    # --------------------------------------------------------
    # CHECK 5: MACD histogram shifting direction or strengthening
    # --------------------------------------------------------
    hist      = ind["macd_hist"]
    hist_prev = ind["macd_hist_prev"]

    if hist is not None and hist_prev is not None:
        direction_change  = (hist > 0 and hist_prev <= 0) or (hist < 0 and hist_prev >= 0)
        # Strengthening: histogram growing by at least 10% of its current value — filters micro-moves
        strengthening     = abs(hist) >= abs(hist_prev) * 1.05 and abs(hist) > 0
        if direction_change or strengthening:
            details["macd_momentum"] = {"pass": True, "value": round(hist, 6), "reason": "MACD momentum shifting"}
            score += 1
        else:
            details["macd_momentum"] = {"pass": False, "value": round(hist, 6), "reason": "MACD flat — no momentum shift"}
    else:
        details["macd_momentum"] = {"pass": False, "value": None, "reason": "MACD data missing"}

    # --------------------------------------------------------
    # CHECK 6: EMA alignment — all 3 in same order
    # --------------------------------------------------------
    bullish = ind["above_ema20"] and ind["ema20_above_50"] and ind["ema50_above_200"]
    bearish = (not ind["above_ema20"]) and (not ind["ema20_above_50"]) and (not ind["ema50_above_200"])

    if bullish or bearish:
        trend = "bullish" if bullish else "bearish"
        details["ema_alignment"] = {"pass": True, "value": trend, "reason": f"Clean {trend} EMA stack"}
        score += 1
    else:
        details["ema_alignment"] = {"pass": False, "value": "mixed", "reason": "EMAs mixed — ranging or transitioning"}

    # --------------------------------------------------------
    # CHECK 7: EMA slope — not flat
    # --------------------------------------------------------
    slope     = ind["ema20_slope"]
    threshold = 0.010 if "JPY" in pair else 0.0001

    if abs(slope) > threshold:
        details["ema_slope"] = {"pass": True, "value": round(slope, 6), "reason": "EMA sloping — directional market"}
        score += 1
    else:
        details["ema_slope"] = {"pass": False, "value": round(slope, 6), "reason": "EMA flat — ranging market"}

    # --------------------------------------------------------
    # CHECK 8: Bollinger Band position supports a direction
    # --------------------------------------------------------
    bb_pct   = ind["bb_pct"]
    bb_width = ind["bb_width"]

    if bb_pct is not None:
        near_lower  = bb_pct < 0.20
        near_upper  = bb_pct > 0.80
        bb_expanding = bb_width > 0.8 if bb_width is not None else False  # bb_width is a %, needs real threshold

        if (near_lower or near_upper) and bb_expanding:
            pos = "lower" if near_lower else "upper"
            details["bb_position"] = {"pass": True, "value": round(bb_pct, 2), "reason": f"Price at BB {pos} with expansion"}
            score += 1
        else:
            details["bb_position"] = {"pass": False, "value": round(bb_pct, 2), "reason": "Price in BB middle or bands contracting"}
    else:
        details["bb_position"] = {"pass": False, "value": None, "reason": "BB data missing"}

    # --------------------------------------------------------
    # CHECK 9: ADX confirms trend strength
    # --------------------------------------------------------
    adx = ind["adx"]
    if adx is not None and adx > 22:
        strength = "strong" if adx > 25 else "moderate"
        details["adx_strength"] = {"pass": True, "value": round(adx, 1), "reason": f"ADX {adx:.1f} — {strength} trend"}
        score += 1
    else:
        adx_val = round(adx, 1) if adx is not None else 0
        details["adx_strength"] = {"pass": False, "value": adx_val, "reason": f"ADX {adx_val} — no clear trend"}

    # --------------------------------------------------------
    # Final score
    # --------------------------------------------------------
    min_score = CONFIG["min_prefilter_score"]
    passed    = score >= min_score

    # Directional bias — full EMA stack + RSI agreement
    if ind["ema20_above_50"] and ind["ema50_above_200"] and ind["above_ema200"] and rsi > 50:
        bias = "BULLISH"
    elif (not ind["ema20_above_50"]) and (not ind["ema50_above_200"]) and (not ind["above_ema200"]) and rsi < 50:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    logger.debug(
        f"{pair} pre-filter: {score}/9 ({'PASS' if passed else 'FAIL'}) bias={bias}"
    )

    return passed, score, details, bias