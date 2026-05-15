# ============================================================
# analyzer.py — Claude Sonnet final signal decision
# Only called after Python pre-filter passes (score >= 6/9)
# Returns structured JSON signal or None on failure.
# ============================================================

import os
import json
import re
import logging
import anthropic
from dotenv import load_dotenv
from config import CONFIG, PIP_SIZE

load_dotenv()
logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


SYSTEM_PROMPT = """You are an expert forex trader with 20 years of systematic trading experience.
You analyze technical setups and give precise, actionable trading signals.

YOUR RULES:
- Only signal HIGH CONVICTION setups. When in doubt: NO TRADE.
- Minimum 65% confidence to signal BUY or SELL.
- Require confluence of at least 3 indicators agreeing.
- Higher timeframe trend (H1) must align with M15 signal — never fight the trend.
- Factor in ATR for realistic SL/TP. ATR-based SL/TP hints are provided.
- Be brutally honest. Weak setup = NO TRADE with a specific reason.
- Never say "it could go either way" — make a decision or say NO TRADE.
- Market ranging (ADX < 20) = NO TRADE unless very strong reversal setup.

RESPOND ONLY IN THIS EXACT JSON FORMAT — no text before or after:
{
  "signal": "BUY" or "SELL" or "NO TRADE",
  "confidence": integer 0-100,
  "entry_price": float or null,
  "stop_loss": float or null,
  "take_profit": float or null,
  "risk_reward": float or null,
  "reasoning": "3-5 sentences, blunt and specific",
  "key_confluences": ["factor1", "factor2", "factor3"],
  "risk_level": "LOW" or "MEDIUM" or "HIGH",
  "warning": "string or null",
  "h1_bias": "BULLISH" or "BEARISH" or "NEUTRAL"
}"""


def _build_prompt(pair, signal_ind, trend_ind, account_balance, spread_pips=None, prefilter_score=None):
    s   = signal_ind
    t   = trend_ind
    pip = PIP_SIZE.get(pair, 0.0001)

    # ATR-based SL/TP for BOTH directions
    atr_sl_pips = round(s["atr"] * CONFIG["sl_atr_multiplier"] / pip, 1)
    atr_tp_pips = round(s["atr"] * CONFIG["tp_atr_multiplier"] / pip, 1)
    price       = s["close"]

    buy_sl  = round(price - s["atr"] * CONFIG["sl_atr_multiplier"], 5)
    buy_tp  = round(price + s["atr"] * CONFIG["tp_atr_multiplier"], 5)
    sell_sl = round(price + s["atr"] * CONFIG["sl_atr_multiplier"], 5)
    sell_tp = round(price - s["atr"] * CONFIG["tp_atr_multiplier"], 5)

    # Session context
    from datetime import datetime
    import pytz
    now     = datetime.now(pytz.UTC)
    hour    = now.hour
    weekday = now.strftime("%A")
    if 7 <= hour < 12:
        session = "London"
    elif 12 <= hour < 16:
        session = "London/NY Overlap (highest liquidity)"
    elif 16 <= hour < 21:
        session = "NY"
    else:
        session = "Off-hours (low liquidity)"

    spread_str = f"{spread_pips:.1f} pips" if spread_pips is not None else "unknown"
    score_str  = f"{prefilter_score}/9" if prefilter_score is not None else "N/A"

    # H1 momentum context
    h1_macd_bias  = "bullish" if t.get("macd_bullish") else "bearish"
    h1_stoch_bias = "K>D bullish" if t.get("stoch_bullish") else "K<D bearish"

    return f"""TRADING SETUP ANALYSIS

PAIR: {pair} | M15 signal | H1 trend
TIME: {now.strftime('%H:%M UTC')} | Day: {weekday} | Session: {session}
ACCOUNT: ${account_balance:.2f} | MAX RISK (1%): ${account_balance * 0.01:.2f}
SPREAD: {spread_str} | PRE-FILTER: {score_str} checks passed

=== H1 TREND (determines direction — never trade against this) ===
Close:     {t['close']:.5f}
EMA20:     {t['ema20']:.5f}  |  EMA50: {t['ema50']:.5f}  |  EMA200: {t['ema200']:.5f}
EMA Stack: {"BULLISH (20>50>200)" if t['ema20_above_50'] and t['ema50_above_200'] else "BEARISH (20<50<200)" if not t['ema20_above_50'] and not t['ema50_above_200'] else "MIXED"}
Price vs EMA200: {"ABOVE" if t['above_ema200'] else "BELOW"}
ADX: {t['adx']:.1f}  |  +DI: {t['dmp']:.1f}  |  -DI: {t['dmn']:.1f}  |  Regime: {t['regime']}
RSI: {t['rsi']:.1f}  |  MACD bias: {h1_macd_bias}  |  Stoch: {h1_stoch_bias}

=== M15 SIGNAL (entry timeframe) ===
Close: {s['close']:.5f}  |  High: {s['high']:.5f}  |  Low: {s['low']:.5f}
Volume ratio (vs 20-bar avg): {s.get('volume_ratio', 1.0):.2f}x  {"(above avg — conviction)" if s.get('volume_ratio', 1.0) > 1.2 else "(below avg — weak)" if s.get('volume_ratio', 1.0) < 0.8 else "(normal)"}

TREND:
EMA20: {s['ema20']:.5f}  |  EMA50: {s['ema50']:.5f}  |  EMA200: {s['ema200']:.5f}
Stack: {"BULLISH" if s['ema20_above_50'] and s['ema50_above_200'] else "BEARISH" if not s['ema20_above_50'] and not s['ema50_above_200'] else "MIXED"}
EMA20 Slope: {"RISING" if s['ema20_slope'] > 0 else "FALLING"} ({s['ema20_slope']:.6f})

MOMENTUM:
RSI: {s['rsi']:.1f}  (prev {s['rsi_prev']:.1f}, {"rising" if s['rsi_rising'] else "falling"})
MACD: {s['macd']:.6f}  |  Signal: {s['macd_signal']:.6f}
Histogram: {s['macd_hist']:.6f}  (prev {s['macd_hist_prev']:.6f}, {"rising" if s['macd_hist_rising'] else "falling"})
Stoch K: {s['stoch_k']:.1f}  |  D: {s['stoch_d']:.1f}  ({"K>D bullish" if s['stoch_bullish'] else "K<D bearish"})

VOLATILITY:
ATR: {s['atr']:.5f}  ({atr_sl_pips:.1f} pip SL | {atr_tp_pips:.1f} pip TP → R:R 1.67)
BB Upper: {s['bb_upper']:.5f}  |  Mid: {s['bb_middle']:.5f}  |  Lower: {s['bb_lower']:.5f}
BB Width: {s['bb_width']:.2f} ({"expanding" if s['bb_width'] > 1.0 else "contracting"})  |  BB%: {s['bb_pct']:.2f}

TREND STRENGTH:
ADX: {s['adx']:.1f}  |  +DI: {s['dmp']:.1f}  |  -DI: {s['dmn']:.1f}
Regime: {s['regime']}

ATR-BASED SL/TP (use these as your starting point, adjust to structure):
IF BUY  → SL: {buy_sl}  | TP: {buy_tp}
IF SELL → SL: {sell_sl} | TP: {sell_tp}
Note: SL must always be BELOW entry for BUY, ABOVE entry for SELL.

RULES:
- If H1 and M15 disagree in direction → NO TRADE
- ADX < 20 on H1 = ranging market → NO TRADE unless very strong reversal
- Spread > 2 pips on this pair eats significantly into R:R — factor it in
- Set entry_price to current close price ({price:.5f})"""


def _safe_parse(text):
    for fn in [
        lambda t: json.loads(t.strip()),
        lambda t: json.loads(re.search(r'\{[\s\S]*\}', t).group()),
        lambda t: json.loads(t.strip().replace("'", '"')),
        lambda t: json.loads(re.sub(r'//.*', '', t)),
    ]:
        try:
            return fn(text)
        except Exception:
            continue
    return None


def analyze(pair, signal_ind, trend_ind, account_balance, spread_pips=None, prefilter_score=None):
    """
    Call Claude to get a trading signal.

    Args:
        pair:             e.g. "EURUSD"
        signal_ind:       dict from indicators.get_latest() on M15
        trend_ind:        dict from indicators.get_latest() on H1
        account_balance:  float
        spread_pips:      current spread in pips (float or None)
        prefilter_score:  pre-filter score 0-9 (int or None)

    Returns:
        signal dict with "pair" key added, or None on failure
    """
    if signal_ind is None or trend_ind is None:
        logger.warning(f"{pair}: missing indicator data")
        return None

    prompt  = _build_prompt(pair, signal_ind, trend_ind, account_balance, spread_pips, prefilter_score)
    client  = _get_client()
    retries = CONFIG["claude_max_retries"]

    for attempt in range(retries + 1):
        try:
            response = client.messages.create(
                model=CONFIG["claude_model"],
                max_tokens=CONFIG["claude_max_tokens"],
                timeout=CONFIG["claude_timeout"],
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            raw_text = response.content[0].text
            signal   = _safe_parse(raw_text)

            if signal is None:
                logger.error(f"{pair}: Claude returned unparseable response:\n{raw_text[:300]}")
                return None

            # Validate required fields
            for field in ["signal", "confidence", "entry_price", "stop_loss", "take_profit", "risk_reward"]:
                if field not in signal:
                    logger.error(f"{pair}: Claude signal missing '{field}'")
                    return None

            # Enforce minimum thresholds
            if signal["signal"] in ("BUY", "SELL"):
                if signal["confidence"] < CONFIG["min_claude_confidence"]:
                    logger.info(f"{pair}: confidence {signal['confidence']}% below threshold — NO TRADE")
                    signal["signal"]  = "NO TRADE"
                    signal["warning"] = f"Confidence {signal['confidence']}% below minimum {CONFIG['min_claude_confidence']}%"
                elif signal.get("risk_reward") and signal["risk_reward"] < CONFIG["min_risk_reward"]:
                    logger.info(f"{pair}: R:R {signal['risk_reward']} below minimum — NO TRADE")
                    signal["signal"]  = "NO TRADE"
                    signal["warning"] = f"R:R {signal['risk_reward']:.2f} below minimum {CONFIG['min_risk_reward']}"
                else:
                    # Validate SL/TP are on correct side of entry
                    entry = signal.get("entry_price") or signal_ind["close"]
                    sl    = signal.get("stop_loss")
                    tp    = signal.get("take_profit")
                    if sl and tp:
                        if signal["signal"] == "BUY" and (sl >= entry or tp <= entry):
                            logger.warning(f"{pair}: BUY signal has inverted SL/TP — NO TRADE (SL={sl}, TP={tp}, entry={entry})")
                            signal["signal"]  = "NO TRADE"
                            signal["warning"] = "SL/TP geometry invalid for BUY — rejected"
                        elif signal["signal"] == "SELL" and (sl <= entry or tp >= entry):
                            logger.warning(f"{pair}: SELL signal has inverted SL/TP — NO TRADE (SL={sl}, TP={tp}, entry={entry})")
                            signal["signal"]  = "NO TRADE"
                            signal["warning"] = "SL/TP geometry invalid for SELL — rejected"

            signal["pair"] = pair
            logger.info(
                f"{pair}: {signal['signal']} | "
                f"confidence {signal.get('confidence')}% | "
                f"R:R {signal.get('risk_reward')}"
            )
            return signal

        except anthropic.APITimeoutError:
            logger.warning(f"{pair}: Claude timeout (attempt {attempt+1})")
            if attempt < retries:
                continue
            return None
        except anthropic.APIStatusError as e:
            logger.error(f"{pair}: Claude API {e.status_code}: {e.message}")
            return None
        except Exception as e:
            logger.error(f"{pair}: Analyzer error: {e}", exc_info=True)
            return None

    return None
