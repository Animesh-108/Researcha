# ============================================================
# backtester.py — Comprehensive Historical Backtester v2.0
#
# Pulls MT5 historical data, replays every M15 candle,
# runs the EXACT same pre-filter + indicator logic as the
# live system, simulates trades with realistic SL/TP,
# and produces a full performance report.
#
# USAGE:
#   python backtester.py                          # all pairs, 6 months
#   python backtester.py --pair EURUSD            # single pair
#   python backtester.py --months 3               # shorter window
#   python backtester.py --balance 500            # different start capital
#   python backtester.py --no-session-filter      # include Asian session
#   python backtester.py --export results.csv     # export trade log to CSV
#   python backtester.py --optimize               # walk-forward threshold test
#   python backtester.py --trailing-sl            # enable trailing stop loss
#   python backtester.py --stress                 # run stress scenarios
#   python backtester.py --monte-carlo            # Monte Carlo simulation
#   python backtester.py --spread-factor 1.5      # widen spread (stress test)
#   python backtester.py --compare-runs           # compare last 5 DB runs
#   python backtester.py --csv-m15 f.csv --csv-h1 f.csv --pair EURUSD
#
# WHAT IT SIMULATES (identical to live system):
#   - 9-point pre-filter (same scanner.py logic, same thresholds from config)
#   - H1 trend alignment: M15 direction must match H1 EMA stack
#   - Session filter (session_start_utc–session_end_utc, no weekends)
#   - ATR-based SL and TP (same sl_atr_multiplier / tp_atr_multiplier)
#   - Spread cost deducted on every entry (realistic per-pair values)
#   - 1% risk per trade position sizing (same executor.py formula)
#   - Correlation lock (EURUSD + GBPUSD not both open simultaneously)
#   - Max 2 open trades simultaneously
#   - Daily loss limit (3%) — mirrors risk_manager.py exactly
#   - Max 3 consecutive losses then 1-hour pause
#   - Drawdown protection tiers (5%/10%/15%/20%)
#   - Candle-accurate SL/TP detection using high/low
#   - Optional trailing stop loss (activates at 1:1, trails at 0.5×ATR)
#
# DOES NOT SIMULATE:
#   - Claude AI signals (pre-filter bias used as direction proxy)
#   - Real news events (calendar unavailable historically)
#   - Partial fills beyond the spread cost
#
# GO / NO-GO CRITERIA (from master plan — all must pass):
#   Win rate > 52%  |  Profit factor > 1.2  |  Max DD < 20%  |  Trades >= 150
# ============================================================

import os
import sys
import gc
import json
import logging
import argparse
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import pytz
import numpy as np
import pandas as pd

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

from config import CONFIG, PIP_SIZE, SYMBOL_MAP, PAIR_CURRENCIES
import indicators as ind_module
from scanner import run_prefilter

logger = logging.getLogger("backtester")

# ============================================================
# Constants — pulled from config so they stay in sync with live
# ============================================================
SESSION_START  = CONFIG["session_start_utc"]
SESSION_END    = CONFIG["session_end_utc"]
SL_MULT        = CONFIG["sl_atr_multiplier"]
TP_MULT        = CONFIG["tp_atr_multiplier"]
RISK_PCT       = CONFIG["risk_per_trade"]
MIN_RR         = CONFIG["min_risk_reward"]
MAX_DD_REDUCE  = CONFIG["drawdown_reduce_size_pct"]
MAX_DD_PAUSE   = CONFIG["drawdown_pause_24h_pct"]
MAX_DD_LOCK    = CONFIG["drawdown_lock_pct"]
MAX_DAILY_LOSS = CONFIG["max_daily_loss_pct"]
MAX_CONSEC     = CONFIG["max_consecutive_losses"]
MAX_OPEN       = CONFIG["max_open_trades"]
CORR_PAIRS     = CONFIG["correlated_pairs"]

# EMA200 warmup: need 200+ bars. Using 250 as safe buffer.
WARMUP_CANDLES = 250
# Extra months to fetch for H1 so EMA200 is valid from the start of the test window
H1_EXTRA_MONTHS = 2

# Realistic Exness typical spreads per pair (pips)
FIXED_SPREAD = {
    "EURUSD": 1.0,
    "GBPUSD": 1.5,
    "USDJPY": 1.0,
}

# Trailing SL parameters
TRAIL_ACTIVATE_MULT = 1.0   # activate after price moves 1×ATR in our favour
TRAIL_STEP_MULT     = 0.5   # trail at 0.5×ATR behind best price


# ============================================================
# Data fetching
# ============================================================

def fetch_mt5_data(pair: str, months: int) -> Optional[dict]:
    """
    Pull M15 + H1 history from MT5 terminal.
    H1 fetch window is extended by H1_EXTRA_MONTHS to ensure
    EMA200 warmup is fully computed before the test period begins.
    Returns {"m15": df, "h1": df} or None on failure.
    """
    try:
        import MetaTrader5 as mt5
        from dotenv import load_dotenv
        load_dotenv()

        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        if not login or not password or not server:
            logger.error("MT5_LOGIN, MT5_PASSWORD, MT5_SERVER must be set in .env")
            return None

        if not mt5.initialize(login=login, password=password, server=server):
            logger.error(f"MT5 init failed: {mt5.last_error()}")
            return None

        symbol = SYMBOL_MAP.get(pair, pair)
        utc_to = datetime.now(pytz.UTC)

        m15_from = utc_to - timedelta(days=months * 31)
        h1_from  = utc_to - timedelta(days=(months + H1_EXTRA_MONTHS) * 31)

        data = {}
        for tf_name, tf_const, from_dt in [
            ("m15", mt5.TIMEFRAME_M15, m15_from),
            ("h1",  mt5.TIMEFRAME_H1,  h1_from),
        ]:
            rates = mt5.copy_rates_range(
                symbol, tf_const,
                from_dt.replace(tzinfo=None),
                utc_to.replace(tzinfo=None),
            )
            if rates is None or len(rates) == 0:
                logger.error(f"{pair} {tf_name}: no data — {mt5.last_error()}")
                mt5.shutdown()
                return None

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})[
                ["time", "open", "high", "low", "close", "volume"]
            ].sort_values("time").reset_index(drop=True)
            data[tf_name] = df
            logger.info(f"{pair} {tf_name}: {len(df)} candles "
                        f"({from_dt.date()} → {utc_to.date()})")

        mt5.shutdown()
        return data

    except ImportError:
        logger.error("MetaTrader5 not installed. Use --csv-m15 / --csv-h1 for CSV mode.")
        return None
    except Exception as e:
        logger.error(f"MT5 data error: {e}", exc_info=True)
        return None


def load_csv_data(pair: str, m15_path: str, h1_path: str) -> Optional[dict]:
    """
    Load M15 and H1 data from CSV files.
    Required columns: time, open, high, low, close, volume
    Time column should be ISO format UTC (e.g. 2024-01-02 08:00:00).
    H1 CSV must have 250+ rows for EMA200 warmup.
    """
    try:
        dfs = {}
        for tf_name, path in [("m15", m15_path), ("h1", h1_path)]:
            df = pd.read_csv(path, parse_dates=["time"])
            if df["time"].dt.tz is None:
                df["time"] = df["time"].dt.tz_localize("UTC")
            for col in ["open", "high", "low", "close", "volume"]:
                if col not in df.columns:
                    logger.error(f"CSV {path} missing required column '{col}'")
                    return None
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["open", "high", "low", "close"]).copy()
            df = df.sort_values("time").reset_index(drop=True)
            dfs[tf_name] = df
            logger.info(f"{pair} {tf_name}: {len(df)} candles from CSV")

        h1_rows = len(dfs["h1"])
        if h1_rows < 250:
            logger.warning(
                f"{pair}: H1 CSV has only {h1_rows} rows — need 250+ for EMA200 warmup. "
                f"Add more H1 history to get accurate backtest results."
            )
        return dfs
    except Exception as e:
        logger.error(f"CSV load error: {e}", exc_info=True)
        return None


# ============================================================
# Session check — mirrors risk_manager.py exactly
# ============================================================

def in_session(dt: datetime, apply: bool = True) -> bool:
    if not apply:
        return True
    if dt.weekday() >= 5:
        return False
    if dt.weekday() == 0 and dt.hour < CONFIG["skip_monday_before"]:
        return False
    if dt.weekday() == 4 and dt.hour >= CONFIG["skip_friday_after"]:
        return False
    return SESSION_START <= dt.hour < SESSION_END


# ============================================================
# SL/TP calculation — mirrors analyzer.py exactly
# ============================================================

def calc_sl_tp(entry: float, direction: str, atr: float, pair: str):
    """
    Returns (sl, tp, risk_reward, sl_pips, tp_pips).
    Uses same ATR multipliers from config as the live analyzer.
    """
    pip   = PIP_SIZE.get(pair, 0.0001)
    sl_d  = atr * SL_MULT
    tp_d  = atr * TP_MULT
    if direction == "BUY":
        sl = round(entry - sl_d, 5)
        tp = round(entry + tp_d, 5)
    else:
        sl = round(entry + sl_d, 5)
        tp = round(entry - tp_d, 5)
    return sl, tp, round(tp_d / sl_d, 2), round(sl_d / pip, 1), round(tp_d / pip, 1)


# ============================================================
# Position sizing — mirrors executor.py exactly
# ============================================================

def calc_lots(balance: float, entry: float, sl: float,
              pair: str, drawdown: float) -> float:
    pip      = PIP_SIZE.get(pair, 0.0001)
    pip_risk = abs(entry - sl) / pip
    if pip_risk < 1:
        return 0.0
    risk_pct = RISK_PCT * (0.5 if drawdown >= MAX_DD_REDUCE else 1.0)
    risk_usd = balance * risk_pct
    pip_val  = (pip / entry) * 100_000 if pair == "USDJPY" else pip * 100_000
    lots     = risk_usd / (pip_risk * pip_val)
    return round(max(0.01, min(lots, 10.0)), 2)


# ============================================================
# P&L calculation
# ============================================================

def calc_pnl(direction: str, actual_entry: float, close_price: float,
             lots: float, pair: str) -> tuple:
    pip      = PIP_SIZE.get(pair, 0.0001)
    raw_diff = close_price - actual_entry
    if direction == "SELL":
        raw_diff = -raw_diff
    pips = raw_diff / pip
    pnl  = raw_diff * lots * 100_000 / close_price if pair == "USDJPY" else raw_diff * lots * 100_000
    return round(pnl, 2), round(pips, 1)


# ============================================================
# Helpers
# ============================================================

def session_label(dt: datetime) -> str:
    h = dt.hour
    if 7  <= h < 12: return "London"
    if 12 <= h < 16: return "Overlap"
    if 16 <= h < 21: return "NY"
    return "Off-hours"


def build_h1_lookup(df_h1_ind: pd.DataFrame):
    """
    Build fast H1 lookup function using binary search.
    Returns closure that maps any numpy timestamp to H1 indicator dict.
    """
    h1_times = df_h1_ind["time"].values.astype("int64")

    def lookup(m15_time_np):
        t   = np.int64(pd.Timestamp(m15_time_np).value)
        idx = int(np.searchsorted(h1_times, t, side="right")) - 1
        if idx < 1:
            return None
        return ind_module.get_latest(df_h1_ind.iloc[idx - 1: idx + 1])
    return lookup


def resolve_direction(bias: str, trend_ind: dict) -> Optional[str]:
    """
    Convert pre-filter bias to BUY/SELL, enforcing H1 EMA alignment.
    Returns None if H1 trend disagrees with M15 bias.
    """
    if bias not in ("BULLISH", "BEARISH"):
        return None
    direction = "BUY" if bias == "BULLISH" else "SELL"
    h1_bull = (trend_ind.get("ema20_above_50") and
               trend_ind.get("ema50_above_200") and
               trend_ind.get("above_ema200"))
    h1_bear = (not trend_ind.get("ema20_above_50") and
               not trend_ind.get("ema50_above_200") and
               not trend_ind.get("above_ema200"))
    if direction == "BUY"  and not h1_bull: return None
    if direction == "SELL" and not h1_bear: return None
    return direction


# ============================================================
# Trailing SL
# ============================================================

def update_trailing_sl(trade: dict, high: float, low: float) -> dict:
    """
    Update trailing SL on an open trade.
    Activates when price moves TRAIL_ACTIVATE_MULT×ATR in our favour.
    Then trails TRAIL_STEP_MULT×ATR behind the best price seen.
    Modifies trade in-place and returns it.
    """
    atr   = trade["atr"]
    entry = trade["actual_entry"]

    if not trade.get("trailing_active"):
        if trade["direction"] == "BUY" and high >= entry + atr * TRAIL_ACTIVATE_MULT:
            trade["trailing_active"] = True
            trade["best_price"]      = high
        elif trade["direction"] == "SELL" and low <= entry - atr * TRAIL_ACTIVATE_MULT:
            trade["trailing_active"] = True
            trade["best_price"]      = low

    if trade.get("trailing_active"):
        if trade["direction"] == "BUY":
            new_best  = max(trade["best_price"], high)
            new_sl    = round(new_best - atr * TRAIL_STEP_MULT, 5)
            trade["best_price"] = new_best
            trade["sl"]         = max(trade["sl"], new_sl)
        else:
            new_best  = min(trade["best_price"], low)
            new_sl    = round(new_best + atr * TRAIL_STEP_MULT, 5)
            trade["best_price"] = new_best
            trade["sl"]         = min(trade["sl"], new_sl)
    return trade


# ============================================================
# Open-trade record builder (shared by both engines)
# ============================================================

def _build_trade_record(pair, direction, ct, entry, actual_entry, sl, tp,
                         sl_p, tp_p, rr, lots, balance, atr, signal_ind,
                         score, sim_spr, dd):
    return {
        "pair":              pair,
        "direction":         direction,
        "open_time":         ct,
        "entry":             entry,
        "actual_entry":      actual_entry,
        "sl":                sl,
        "tp":                tp,
        "sl_pips":           sl_p,
        "tp_pips":           tp_p,
        "rr":                rr,
        "lots":              lots,
        "risk_usd":          round(balance * RISK_PCT, 2),
        "atr":               round(atr, 6),
        "rsi":               round(signal_ind["rsi"], 1),
        "adx":               round(signal_ind["adx"], 1),
        "regime":            signal_ind["regime"],
        "macd_hist":         round(signal_ind["macd_hist"], 6),
        "prefilter_score":   score,
        "session":           session_label(ct),
        "day_of_week":       ct.strftime("%A"),
        "hour_utc":          ct.hour,
        "spread_pips":       sim_spr,
        "drawdown_at_entry": round(dd * 100, 2),
        "trailing_active":   False,
        "best_price":        actual_entry,
    }


# ============================================================
# Single-pair backtest engine
# ============================================================

def backtest_pair(pair: str, data: dict, start_balance: float,
                  apply_session: bool = True, verbose: bool = False,
                  use_trailing_sl: bool = False,
                  spread_factor: float = 1.0) -> dict:
    """
    Full candle-by-candle replay for one pair.
    Returns result dict with trades, equity_curve, final_balance.
    """
    df_m15_ind = ind_module.calculate_all(data["m15"].copy().reset_index(drop=True))
    df_h1_ind  = ind_module.calculate_all(data["h1"].copy().reset_index(drop=True))

    if df_m15_ind is None:
        logger.error(f"{pair}: M15 indicator fail (need 200+ candles, have {len(data['m15'])})")
        return {"pair": pair, "trades": [], "equity_curve": [],
                "final_balance": start_balance, "start_balance": start_balance,
                "error": "m15_indicator_fail"}

    if df_h1_ind is None:
        logger.error(
            f"{pair}: H1 indicator fail — need 250+ H1 candles for EMA200 warmup, "
            f"have {len(data['h1'])}. Use --months with a longer period."
        )
        return {"pair": pair, "trades": [], "equity_curve": [],
                "final_balance": start_balance, "start_balance": start_balance,
                "error": "h1_indicator_fail"}

    logger.info(f"{pair}: {len(df_m15_ind)} usable M15 candles after warmup")
    get_h1  = build_h1_lookup(df_h1_ind)
    sim_spr = FIXED_SPREAD.get(pair, 1.0) * spread_factor

    balance = start_balance; peak_bal = start_balance
    daily_start_bal = start_balance; daily_date = None; daily_loss_pct = 0.0
    consec_loss = 0; open_trade = None; paused_i = -1
    trades = []; equity_curve = []

    progress = range(WARMUP_CANDLES, len(df_m15_ind))
    if HAS_TQDM:
        progress = tqdm(progress, desc=pair, unit="candle", leave=False)

    for i in progress:
        row = df_m15_ind.iloc[i]
        ct  = pd.Timestamp(row["time"]).to_pydatetime()
        if ct.tzinfo is None:
            ct = pytz.UTC.localize(ct)

        if daily_date != ct.date():
            daily_date = ct.date(); daily_start_bal = balance; daily_loss_pct = 0.0

        # ── Check open trade ──────────────────────────────
        if open_trade is not None:
            if use_trailing_sl:
                open_trade = update_trailing_sl(open_trade, float(row["high"]), float(row["low"]))

            ot     = open_trade
            sl_hit = row["low"]  <= ot["sl"] if ot["direction"] == "BUY" else row["high"] >= ot["sl"]
            tp_hit = row["high"] >= ot["tp"] if ot["direction"] == "BUY" else row["low"]  <= ot["tp"]
            if sl_hit and tp_hit: sl_hit, tp_hit = True, False   # conservative

            if sl_hit or tp_hit:
                cp        = ot["sl"] if sl_hit else ot["tp"]
                pnl, pips = calc_pnl(ot["direction"], ot["actual_entry"], cp, ot["lots"], pair)
                outcome   = "WIN" if tp_hit else "LOSS"
                balance  += pnl; peak_bal = max(peak_bal, balance)
                if balance < daily_start_bal:
                    daily_loss_pct = (daily_start_bal - balance) / daily_start_bal
                consec_loss = 0 if pnl > 0 else consec_loss + 1
                dur_m = int((ct - ot["open_time"]).total_seconds() / 60)
                dd_c  = round((peak_bal - balance) / peak_bal * 100, 2) if peak_bal > 0 else 0

                trades.append({**ot, "outcome": outcome, "close_price": cp, "close_time": ct,
                                "pips": pips, "pnl": pnl, "balance_after": round(balance, 2),
                                "duration_mins": dur_m, "drawdown_at_close": dd_c,
                                "trailing_used": ot.get("trailing_active", False),
                                "close_reason": "TP" if tp_hit else "SL"})
                equity_curve.append((ct, round(balance, 2)))
                open_trade = None
                if verbose:
                    print(f"  {ct:%Y-%m-%d %H:%M} {pair} {ot['direction']} "
                          f"{outcome} {pips:+.1f}pip ${pnl:+.2f} bal=${balance:.2f}")

        # ── Risk gates ────────────────────────────────────
        dd = (peak_bal - balance) / peak_bal if peak_bal > 0 else 0
        if dd >= MAX_DD_LOCK:            continue
        if i < paused_i:                 continue
        if daily_loss_pct >= MAX_DAILY_LOSS: continue
        if consec_loss >= MAX_CONSEC:    paused_i = i + 4; consec_loss = 0; continue
        if dd >= MAX_DD_PAUSE:           paused_i = i + 96; continue
        if open_trade is not None:       continue
        if not in_session(ct, apply_session): continue

        # ── Indicators ───────────────────────────────────
        signal_ind = ind_module.get_latest(df_m15_ind.iloc[max(0, i-1): i+1])
        trend_ind  = get_h1(row["time"])
        if signal_ind is None or trend_ind is None: continue

        # ── Pre-filter + direction ────────────────────────
        passed, score, _, bias = run_prefilter(pair, signal_ind, sim_spr)
        if not passed: continue
        direction = resolve_direction(bias, trend_ind)
        if direction is None: continue

        # ── Entry, SL/TP, lots ────────────────────────────
        entry = float(row["close"]); atr = signal_ind["atr"]
        sl, tp, rr, sl_p, tp_p = calc_sl_tp(entry, direction, atr, pair)
        if rr < MIN_RR: continue

        spr_price    = sim_spr * PIP_SIZE.get(pair, 0.0001)
        actual_entry = entry + spr_price if direction == "BUY" else entry - spr_price
        lots         = calc_lots(balance, actual_entry, sl, pair, dd)
        if lots <= 0: continue

        open_trade = _build_trade_record(
            pair, direction, ct, entry, actual_entry, sl, tp,
            sl_p, tp_p, rr, lots, balance, atr, signal_ind, score, sim_spr, dd)

    # ── Close residual open trade ─────────────────────────
    if open_trade is not None:
        last      = df_m15_ind.iloc[-1]
        cp        = float(last["close"])
        pnl, pips = calc_pnl(open_trade["direction"], open_trade["actual_entry"],
                              cp, open_trade["lots"], pair)
        balance  += pnl
        trades.append({**open_trade, "outcome": "WIN" if pnl > 0 else "LOSS",
                       "close_price": cp, "close_time": pd.Timestamp(last["time"]).to_pydatetime(),
                       "pips": pips, "pnl": pnl, "balance_after": round(balance, 2),
                       "duration_mins": None,
                       "drawdown_at_close": round((peak_bal - balance) / peak_bal * 100, 2) if peak_bal > 0 else 0,
                       "trailing_used": open_trade.get("trailing_active", False),
                       "close_reason": "EOD"})

    return {"pair": pair, "trades": trades, "equity_curve": equity_curve,
            "final_balance": round(balance, 2), "start_balance": start_balance}


# ============================================================
# Multi-pair engine with correlation enforcement
# ============================================================

def backtest_all_pairs(pairs_data: dict, start_balance: float,
                       apply_session: bool = True, verbose: bool = False,
                       use_trailing_sl: bool = False,
                       spread_factor: float = 1.0) -> dict:
    """
    Simultaneous multi-pair backtest on a shared time axis.
    Enforces correlation lock and max 2 open trades across all pairs.
    """
    all_ind   = {}
    all_h1_fn = {}

    for pair, data in pairs_data.items():
        m15i = ind_module.calculate_all(data["m15"].copy().reset_index(drop=True))
        h1i  = ind_module.calculate_all(data["h1"].copy().reset_index(drop=True))
        if m15i is None:
            logger.error(f"{pair}: M15 indicator fail — skipping")
            continue
        if h1i is None:
            logger.error(f"{pair}: H1 indicator fail (need 250+ rows) — skipping")
            continue
        all_ind[pair]   = m15i
        all_h1_fn[pair] = build_h1_lookup(h1i)
        logger.info(f"{pair}: {len(m15i)} usable M15 candles")

    if not all_ind:
        logger.error("No pairs with valid indicator data")
        return {"error": "no_data", "trades": [], "equity_curve": [],
                "final_balance": start_balance, "start_balance": start_balance}

    ref_pair  = max(all_ind, key=lambda p: len(all_ind[p]))
    all_times = all_ind[ref_pair]["time"].values

    balance = start_balance; peak_bal = start_balance
    daily_start_bal = start_balance; daily_date = None; daily_loss_pct = 0.0
    consec_loss = 0; open_trades = {}; paused_i = -1
    all_trades = []; equity_curve = []

    progress = range(WARMUP_CANDLES, len(all_times))
    if HAS_TQDM:
        progress = tqdm(progress, desc="Multi-pair", unit="candle", leave=False)

    for i in progress:
        ct = pd.Timestamp(all_times[i]).to_pydatetime()
        if ct.tzinfo is None:
            ct = pytz.UTC.localize(ct)

        if daily_date != ct.date():
            daily_date = ct.date(); daily_start_bal = balance; daily_loss_pct = 0.0

        # ── Check all open trades ─────────────────────────
        for pair in list(open_trades.keys()):
            df_p = all_ind[pair]
            mask = df_p["time"].values <= all_times[i]
            if not mask.any(): continue
            row = df_p.iloc[int(np.where(mask)[0][-1])]
            ot  = open_trades[pair]

            if use_trailing_sl:
                ot = update_trailing_sl(ot, float(row["high"]), float(row["low"]))

            sl_hit = row["low"]  <= ot["sl"] if ot["direction"] == "BUY" else row["high"] >= ot["sl"]
            tp_hit = row["high"] >= ot["tp"] if ot["direction"] == "BUY" else row["low"]  <= ot["tp"]
            if sl_hit and tp_hit: sl_hit, tp_hit = True, False

            if sl_hit or tp_hit:
                cp        = ot["sl"] if sl_hit else ot["tp"]
                pnl, pips = calc_pnl(ot["direction"], ot["actual_entry"], cp, ot["lots"], pair)
                outcome   = "WIN" if tp_hit else "LOSS"
                balance  += pnl; peak_bal = max(peak_bal, balance)
                if balance < daily_start_bal:
                    daily_loss_pct = (daily_start_bal - balance) / daily_start_bal
                consec_loss = 0 if pnl > 0 else consec_loss + 1
                dur_m = int((ct - ot["open_time"]).total_seconds() / 60)
                dd_c  = round((peak_bal - balance) / peak_bal * 100, 2) if peak_bal > 0 else 0

                all_trades.append({**ot, "outcome": outcome, "close_price": cp, "close_time": ct,
                                   "pips": pips, "pnl": pnl, "balance_after": round(balance, 2),
                                   "duration_mins": dur_m, "drawdown_at_close": dd_c,
                                   "trailing_used": ot.get("trailing_active", False),
                                   "close_reason": "TP" if tp_hit else "SL"})
                equity_curve.append((ct, round(balance, 2)))
                del open_trades[pair]

                if verbose:
                    print(f"  {ct:%Y-%m-%d %H:%M} {pair} {ot['direction']} "
                          f"{outcome} {pips:+.1f}pip ${pnl:+.2f} bal=${balance:.2f}")

        # ── Risk gates ────────────────────────────────────
        dd = (peak_bal - balance) / peak_bal if peak_bal > 0 else 0
        if dd >= MAX_DD_LOCK:              continue
        if i < paused_i:                   continue
        if daily_loss_pct >= MAX_DAILY_LOSS: continue
        if consec_loss >= MAX_CONSEC:      paused_i = i + 4; consec_loss = 0; continue
        if dd >= MAX_DD_PAUSE:             paused_i = i + 96; continue
        if len(open_trades) >= MAX_OPEN:   continue
        if not in_session(ct, apply_session): continue

        # ── Scan each pair ────────────────────────────────
        for pair in list(all_ind.keys()):
            if pair in open_trades:                              continue
            if any(pair in CORR_PAIRS.get(op, []) for op in open_trades): continue
            if len(open_trades) >= MAX_OPEN:                     break

            df_p = all_ind[pair]
            mask = df_p["time"].values <= all_times[i]
            if not mask.any(): continue
            last_idx = int(np.where(mask)[0][-1])
            if last_idx < WARMUP_CANDLES: continue

            row        = df_p.iloc[last_idx]
            signal_ind = ind_module.get_latest(df_p.iloc[max(0, last_idx-1): last_idx+1])
            trend_ind  = all_h1_fn[pair](all_times[i])
            if signal_ind is None or trend_ind is None: continue

            sim_spr        = FIXED_SPREAD.get(pair, 1.0) * spread_factor
            passed, score, _, bias = run_prefilter(pair, signal_ind, sim_spr)
            if not passed: continue
            direction = resolve_direction(bias, trend_ind)
            if direction is None: continue

            entry = float(row["close"]); atr = signal_ind["atr"]
            sl, tp, rr, sl_p, tp_p = calc_sl_tp(entry, direction, atr, pair)
            if rr < MIN_RR: continue

            spr_price    = sim_spr * PIP_SIZE.get(pair, 0.0001)
            actual_entry = entry + spr_price if direction == "BUY" else entry - spr_price
            lots         = calc_lots(balance, actual_entry, sl, pair, dd)
            if lots <= 0: continue

            open_trades[pair] = _build_trade_record(
                pair, direction, ct, entry, actual_entry, sl, tp,
                sl_p, tp_p, rr, lots, balance, atr, signal_ind, score, sim_spr, dd)

    # ── Close residual open trades ────────────────────────
    for pair, ot in list(open_trades.items()):
        last      = all_ind[pair].iloc[-1]
        cp        = float(last["close"])
        pnl, pips = calc_pnl(ot["direction"], ot["actual_entry"], cp, ot["lots"], pair)
        balance  += pnl
        all_trades.append({**ot, "outcome": "WIN" if pnl > 0 else "LOSS",
                           "close_price": cp,
                           "close_time": pd.Timestamp(last["time"]).to_pydatetime(),
                           "pips": pips, "pnl": pnl, "balance_after": round(balance, 2),
                           "duration_mins": None,
                           "drawdown_at_close": round((peak_bal - balance) / peak_bal * 100, 2) if peak_bal > 0 else 0,
                           "trailing_used": ot.get("trailing_active", False),
                           "close_reason": "EOD"})

    return {"trades": all_trades, "equity_curve": equity_curve,
            "final_balance": round(balance, 2), "start_balance": start_balance}


# ============================================================
# Statistics engine — comprehensive
# ============================================================

def compute_stats(trades: list, start_balance: float) -> dict:
    if not trades:
        return {"total": 0, "error": "no_trades"}

    df    = pd.DataFrame(trades)
    total = len(df)
    wins  = int((df["outcome"] == "WIN").sum())
    losses = total - wins
    win_rate = wins / total

    pnls          = df["pnl"].fillna(0).astype(float)
    total_pnl     = round(float(pnls.sum()), 2)
    avg_win_pnl   = round(float(df.loc[df["outcome"]=="WIN",  "pnl"].mean()), 2) if wins   > 0 else 0.0
    avg_loss_pnl  = round(float(df.loc[df["outcome"]=="LOSS", "pnl"].mean()), 2) if losses > 0 else 0.0
    gross_profit  = float(df.loc[df["pnl"]>0, "pnl"].sum())
    gross_loss    = abs(float(df.loc[df["pnl"]<0, "pnl"].sum()))
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else float("inf")
    expectancy    = round(win_rate * avg_win_pnl + (1-win_rate) * avg_loss_pnl, 2)

    # Maximum drawdown on equity curve
    bal_arr     = np.array([start_balance] + list(df["balance_after"].dropna()))
    running_max = np.maximum.accumulate(bal_arr)
    dd_arr      = (running_max - bal_arr) / running_max * 100
    max_dd      = round(float(dd_arr.max()), 2)

    # Calmar ratio
    total_ret_pct = total_pnl / start_balance * 100
    calmar  = round(total_ret_pct / max_dd, 2) if max_dd > 0 else 0.0

    # Sharpe approximation
    sharpe = 0.0
    if len(pnls) > 1 and float(pnls.std()) > 0:
        sharpe = round((float(pnls.mean()) / float(pnls.std())) * (252**0.5), 2)

    # Streaks
    outcomes = list(df["outcome"])
    max_ws = max_ls = cur_w = cur_l = 0
    for o in outcomes:
        if o == "WIN": cur_w += 1; cur_l = 0
        else:          cur_l += 1; cur_w = 0
        max_ws = max(max_ws, cur_w); max_ls = max(max_ls, cur_l)

    avg_win_pips  = round(float(df.loc[df["outcome"]=="WIN",  "pips"].mean()), 1) if wins   > 0 else 0.0
    avg_loss_pips = round(float(df.loc[df["outcome"]=="LOSS", "pips"].mean()), 1) if losses > 0 else 0.0
    avg_dur       = round(float(df["duration_mins"].dropna().mean()), 0) if "duration_mins" in df else None
    trailing_cnt  = int(df["trailing_used"].sum()) if "trailing_used" in df.columns else 0

    # Wilson 95% CI for win rate
    z   = 1.96
    p   = win_rate
    den = 1 + z*z/total
    mid = (p + z*z/(2*total)) / den
    rad = (z * (p*(1-p)/total + z*z/(4*total*total))**0.5) / den
    wr_low  = round((mid - rad) * 100, 1)
    wr_high = round((mid + rad) * 100, 1)

    def breakdown(col):
        if col not in df.columns: return {}
        out = {}
        for val, grp in df.groupby(col):
            w = int((grp["outcome"]=="WIN").sum()); t = len(grp)
            out[str(val)] = {"trades": t, "wins": w,
                             "win_rate":  round(w/t*100, 1),
                             "total_pnl": round(float(grp["pnl"].sum()), 2),
                             "avg_pnl":   round(float(grp["pnl"].mean()), 2)}
        return out

    monthly = {}
    if "open_time" in df.columns:
        try:
            df["_m"] = pd.to_datetime(df["open_time"]).dt.to_period("M").astype(str)
            for m, grp in df.groupby("_m"):
                w = int((grp["outcome"]=="WIN").sum()); t = len(grp)
                monthly[str(m)] = {"trades": t, "wins": w,
                                   "win_rate": round(w/t*100,1),
                                   "pnl": round(float(grp["pnl"].sum()),2)}
        except Exception:
            pass

    gng = {
        "win_rate_pass":      bool(win_rate >= 0.52),
        "profit_factor_pass": bool(profit_factor >= 1.2),
        "max_dd_pass":        bool(max_dd < 20.0),
        "min_trades_pass":    bool(total >= 150),
    }

    return {
        "total": total, "wins": wins, "losses": losses,
        "win_rate_pct":      round(win_rate * 100, 1),
        "win_rate_ci_low":   wr_low,
        "win_rate_ci_high":  wr_high,
        "total_pnl":         total_pnl,
        "total_return_pct":  round(total_ret_pct, 2),
        "avg_win":           avg_win_pnl,
        "avg_loss":          avg_loss_pnl,
        "avg_pnl":           round(float(pnls.mean()), 2),
        "profit_factor":     profit_factor,
        "expectancy":        expectancy,
        "max_drawdown_pct":  max_dd,
        "calmar_ratio":      calmar,
        "sharpe_approx":     sharpe,
        "max_win_streak":    max_ws,
        "max_loss_streak":   max_ls,
        "avg_win_pips":      avg_win_pips,
        "avg_loss_pips":     avg_loss_pips,
        "avg_duration_mins": avg_dur,
        "trailing_sl_used":  trailing_cnt,
        "by_pair":           breakdown("pair"),
        "by_session":        breakdown("session"),
        "by_day":            breakdown("day_of_week"),
        "by_regime":         breakdown("regime"),
        "by_score":          breakdown("prefilter_score"),
        "by_close_reason":   breakdown("close_reason"),
        "by_month":          monthly,
        "go_nogo":           gng,
    }


# ============================================================
# Walk-forward optimizer
# ============================================================

def walk_forward_optimize(pairs_data: dict, start_balance: float,
                           train_months: int = 3,
                           test_months:  int = 1) -> list:
    """
    Walk-forward optimization.
    Grid searches prefilter threshold on training window,
    validates on unseen out-of-sample test window.
    Prevents overfitting.
    """
    logger.info(f"Walk-forward: train={train_months}mo test={test_months}mo")
    ref_pair   = list(pairs_data.keys())[0]
    all_times  = pairs_data[ref_pair]["m15"]["time"].values
    total_days = int((pd.Timestamp(all_times[-1]) - pd.Timestamp(all_times[0])).total_seconds() / 86400)
    train_d    = train_months * 30
    test_d     = test_months  * 30
    orig       = CONFIG["min_prefilter_score"]

    def slice_data(start_dt, end_dt):
        out = {}
        for p, d in pairs_data.items():
            # Extend H1 backward for EMA200 warmup
            h1_start = start_dt - pd.Timedelta(days=H1_EXTRA_MONTHS * 31)
            out[p] = {
                "m15": d["m15"][(d["m15"]["time"] >= start_dt) & (d["m15"]["time"] < end_dt)].copy(),
                "h1":  d["h1"][ (d["h1"]["time"]  >= h1_start) & (d["h1"]["time"]  < end_dt)].copy(),
            }
        return out

    windows = []
    cursor  = 0

    while cursor + train_d + test_d <= total_days:
        t0 = pd.Timestamp(all_times[0]) + pd.Timedelta(days=cursor)
        t1 = t0 + pd.Timedelta(days=train_d)
        t2 = t1 + pd.Timedelta(days=test_d)

        logger.info(f"  Train {t0.date()}→{t1.date()}  Test {t1.date()}→{t2.date()}")

        # Grid search on training window
        best_thresh = 7; best_pf = 0.0
        for thresh in [6, 7, 8]:
            CONFIG["min_prefilter_score"] = thresh
            res = backtest_all_pairs(slice_data(t0, t1), start_balance)
            st  = compute_stats(res.get("trades", []), start_balance)
            pf  = st.get("profit_factor", 0)
            cnt = st.get("total", 0)
            logger.info(f"    thresh={thresh} trades={cnt} PF={pf:.3f}")
            if isinstance(pf, float) and pf > best_pf and cnt >= 5:
                best_pf, best_thresh = pf, thresh

        logger.info(f"  ✓ Best thresh={best_thresh} (train PF={best_pf:.3f})")

        # Out-of-sample test
        CONFIG["min_prefilter_score"] = best_thresh
        test_res   = backtest_all_pairs(slice_data(t1, t2), start_balance)
        test_stats = compute_stats(test_res.get("trades", []), start_balance)

        windows.append({
            "train_start":    str(t0.date()),
            "train_end":      str(t1.date()),
            "test_start":     str(t1.date()),
            "test_end":       str(t2.date()),
            "best_threshold": best_thresh,
            "train_pf":       round(best_pf, 3),
            "test_trades":    test_stats.get("total", 0),
            "test_win_rate":  test_stats.get("win_rate_pct", 0),
            "test_pf":        test_stats.get("profit_factor", 0),
            "test_pnl":       test_stats.get("total_pnl", 0),
            "test_max_dd":    test_stats.get("max_drawdown_pct", 0),
        })
        CONFIG["min_prefilter_score"] = orig
        cursor += test_d

    return windows


# ============================================================
# Monte Carlo simulation
# ============================================================

def monte_carlo(trades: list, start_balance: float,
                simulations: int = 1000, seed: int = 42) -> dict:
    """
    Randomise trade ordering N times.
    If real edge depends heavily on sequence, the system is fragile.
    Returns percentile distribution of outcomes.
    """
    if len(trades) < 10:
        return {}

    np.random.seed(seed)
    pnls = np.array([t.get("pnl", 0) for t in trades], dtype=float)
    n    = len(pnls)

    final_bals   = []
    max_dds      = []
    loss_streaks = []

    for _ in range(simulations):
        order  = np.random.choice(pnls, size=n, replace=False)
        bal    = start_balance
        peak   = start_balance
        max_dd = 0.0
        ls = cur_l = 0

        for p in order:
            bal  += p
            peak  = max(peak, bal)
            dd    = (peak - bal) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
            cur_l  = 0 if p > 0 else cur_l + 1
            ls     = max(ls, cur_l)

        final_bals.append(bal)
        max_dds.append(max_dd)
        loss_streaks.append(ls)

    def pct(arr, p):
        return round(float(np.percentile(arr, p)), 2)

    return {
        "simulations": simulations,
        "final_balance": {
            "p5":  pct(final_bals, 5),  "p25": pct(final_bals, 25),
            "p50": pct(final_bals, 50), "p75": pct(final_bals, 75),
            "p95": pct(final_bals, 95),
            "pct_profitable": round(sum(1 for b in final_bals if b > start_balance) / simulations * 100, 1),
        },
        "max_drawdown": {
            "p50": pct(max_dds, 50), "p75": pct(max_dds, 75), "p95": pct(max_dds, 95),
        },
        "max_loss_streak": {
            "p50": pct(loss_streaks, 50), "p75": pct(loss_streaks, 75), "p95": pct(loss_streaks, 95),
        },
    }


# ============================================================
# Stress tests
# ============================================================

def run_stress_tests(pairs_data: dict, start_balance: float,
                      apply_session: bool = True) -> dict:
    """
    Three stress scenarios:
    1. Wide spread (1.5×) — simulates adverse market conditions
    2. No session filter   — includes Asian session
    3. Trailing SL enabled — compares with/without trailing
    """
    scenarios = {}

    logger.info("Stress 1: wide spread 1.5×")
    r = backtest_all_pairs(pairs_data, start_balance,
                           apply_session=apply_session, spread_factor=1.5)
    scenarios["wide_spread_1.5x"] = compute_stats(r.get("trades", []), start_balance)

    logger.info("Stress 2: no session filter")
    r = backtest_all_pairs(pairs_data, start_balance, apply_session=False)
    scenarios["no_session_filter"] = compute_stats(r.get("trades", []), start_balance)

    logger.info("Stress 3: trailing SL on")
    r = backtest_all_pairs(pairs_data, start_balance,
                           apply_session=apply_session, use_trailing_sl=True)
    scenarios["trailing_sl_on"] = compute_stats(r.get("trades", []), start_balance)

    return scenarios


# ============================================================
# Report printer
# ============================================================

def print_report(stats: dict, start_balance: float,
                  mc: dict = None, stress: dict = None):
    sep  = "=" * 66
    sep2 = "-" * 66
    yn   = lambda v: "✓ PASS" if v else "✗ FAIL"

    print(f"\n{sep}")
    print("  BACKTEST RESULTS")
    print(sep)

    gng = stats.get("go_nogo", {})
    print("\n  GO / NO-GO  (all must pass before demo trading)")
    print(sep2)
    print(f"  Win rate >= 52%:          {stats['win_rate_pct']:>5.1f}%  [{yn(gng.get('win_rate_pass'))}]")
    print(f"  Win rate 95% CI:          {stats.get('win_rate_ci_low',0):.1f}% – {stats.get('win_rate_ci_high',0):.1f}%")
    print(f"  Profit factor >= 1.2:     {stats['profit_factor']:>6.3f}  [{yn(gng.get('profit_factor_pass'))}]")
    print(f"  Max drawdown < 20%:       {stats['max_drawdown_pct']:>5.1f}%  [{yn(gng.get('max_dd_pass'))}]")
    print(f"  Total trades >= 150:      {stats['total']:>5}    [{yn(gng.get('min_trades_pass'))}]")
    all_pass = all(gng.values())
    print(f"\n  VERDICT: {'✓' if all_pass else '✗'} "
          f"{'PROCEED TO DEMO TRADING' if all_pass else 'DO NOT TRADE — fix failing criteria'}")

    print(f"\n{sep2}")
    print("  CORE METRICS")
    print(sep2)
    fin_bal = start_balance + stats["total_pnl"]
    print(f"  Starting balance:         ${start_balance:>10,.2f}")
    print(f"  Final balance:            ${fin_bal:>10,.2f}")
    print(f"  Total trades:             {stats['total']:>10}")
    print(f"  Wins / Losses:            {stats['wins']:>5} / {stats['losses']}")
    print(f"  Win rate:                 {stats['win_rate_pct']:>9.1f}%")
    print(f"  Total P&L:                ${stats['total_pnl']:>+10,.2f}")
    print(f"  Total return:             {stats['total_return_pct']:>+9.2f}%")
    print(f"  Profit factor:            {stats['profit_factor']:>10.3f}")
    print(f"  Expectancy/trade:         ${stats['expectancy']:>+9.2f}")
    print(f"  Avg win:                  ${stats['avg_win']:>+9.2f}  ({stats['avg_win_pips']:+.1f} pips)")
    print(f"  Avg loss:                 ${stats['avg_loss']:>+9.2f}  ({stats['avg_loss_pips']:+.1f} pips)")
    print(f"  Max drawdown:             {stats['max_drawdown_pct']:>9.1f}%")
    print(f"  Calmar ratio:             {stats.get('calmar_ratio',0):>10.2f}")
    print(f"  Sharpe (approx):          {stats['sharpe_approx']:>10.2f}")
    print(f"  Max win streak:           {stats['max_win_streak']:>10}")
    print(f"  Max loss streak:          {stats['max_loss_streak']:>10}")
    if stats.get("avg_duration_mins"):
        h, m = divmod(int(stats["avg_duration_mins"]), 60)
        print(f"  Avg trade duration:       {h:>7}h {m:02d}m")
    if stats.get("trailing_sl_used"):
        print(f"  Trailing SL triggered:    {stats['trailing_sl_used']:>10} trades")

    for title, key, col in [
        ("BY PAIR",             "by_pair",        "Pair"),
        ("BY SESSION",          "by_session",      "Session"),
        ("BY DAY OF WEEK",      "by_day",          "Day"),
        ("BY MARKET REGIME",    "by_regime",       "Regime"),
        ("BY PRE-FILTER SCORE", "by_score",        "Score"),
        ("BY CLOSE REASON",     "by_close_reason", "Reason"),
    ]:
        data = stats.get(key)
        if not data: continue
        print(f"\n{sep2}")
        print(f"  {title}")
        print(sep2)
        print(f"  {col:<18} {'Trades':>7} {'WR%':>6} {'P&L':>10} {'Avg':>8}")
        for k in sorted(data):
            d = data[k]
            print(f"  {str(k):<18} {d['trades']:>7} {d['win_rate']:>5.1f}%"
                  f" {d['total_pnl']:>+10.2f} {d['avg_pnl']:>+8.2f}")

    if stats.get("by_month"):
        print(f"\n{sep2}")
        print("  BY MONTH")
        print(sep2)
        print(f"  {'Month':<12} {'Trades':>7} {'WR%':>6} {'P&L':>10}")
        for m in sorted(stats["by_month"]):
            d = stats["by_month"][m]
            print(f"  {m:<12} {d['trades']:>7} {d['win_rate']:>5.1f}% {d['pnl']:>+10.2f}")

    if mc and mc.get("simulations"):
        print(f"\n{sep2}")
        print(f"  MONTE CARLO  ({mc['simulations']} randomised sequences)")
        print(sep2)
        fb = mc["final_balance"]
        dd = mc["max_drawdown"]
        ls = mc["max_loss_streak"]
        print(f"  Final balance  P5:   ${fb['p5']:>10,.2f}  (worst 5% of runs)")
        print(f"                 P50:  ${fb['p50']:>10,.2f}  (median)")
        print(f"                 P95:  ${fb['p95']:>10,.2f}  (best 5%)")
        print(f"  Profitable:    {fb['pct_profitable']:>5.1f}% of simulations end in profit")
        print(f"  Max drawdown   P50:  {dd['p50']:>5.1f}%   P95:  {dd['p95']:.1f}%")
        print(f"  Max loss run   P50:  {ls['p50']:>5}    P95:  {ls['p95']}")

    if stress:
        print(f"\n{sep2}")
        print("  STRESS TESTS")
        print(sep2)
        print(f"  {'Scenario':<28} {'Trades':>7} {'WR%':>6} {'PF':>6} {'MaxDD':>7} {'P&L':>10}")
        for name, s in stress.items():
            if s.get("total", 0) == 0:
                print(f"  {name:<28} {'no trades':>7}"); continue
            print(f"  {name:<28} {s['total']:>7} {s['win_rate_pct']:>5.1f}%"
                  f" {s['profit_factor']:>6.3f} {s['max_drawdown_pct']:>6.1f}%"
                  f" {s['total_pnl']:>+10.2f}")

    print(f"\n{sep}\n")


# ============================================================
# Exports
# ============================================================

def export_csv(trades: list, path: str):
    if not trades:
        logger.warning("No trades to export"); return
    df = pd.DataFrame(trades)
    for col in ["open_time", "close_time"]:
        if col in df.columns:
            df[col] = df[col].astype(str)
    df.to_csv(path, index=False)
    logger.info(f"CSV exported: {path}  ({len(trades)} rows)")


def export_sqlite(trades: list, stats: dict, db_path: str,
                   mc: dict = None, stress: dict = None) -> int:
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS backtest_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_time        TEXT, pairs TEXT, start_balance REAL,
        total_trades    INTEGER, win_rate REAL, profit_factor REAL,
        total_pnl REAL, max_drawdown REAL, sharpe REAL, calmar REAL,
        verdict TEXT, go_nogo TEXT, monte_carlo TEXT,
        stress_tests TEXT, full_stats TEXT)""")

    cur.execute("""CREATE TABLE IF NOT EXISTS backtest_trades (
        id                INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
        pair TEXT, direction TEXT, open_time TEXT, close_time TEXT,
        entry REAL, actual_entry REAL, sl REAL, tp REAL,
        lots REAL, rr REAL, sl_pips REAL, tp_pips REAL,
        session TEXT, day_of_week TEXT, hour_utc INTEGER,
        regime TEXT, rsi REAL, adx REAL, macd_hist REAL, atr REAL,
        prefilter_score INTEGER, spread_pips REAL, drawdown_at_entry REAL,
        outcome TEXT, close_reason TEXT, close_price REAL,
        pips REAL, pnl REAL, balance_after REAL,
        duration_mins REAL, drawdown_at_close REAL, trailing_used INTEGER)""")

    gng     = stats.get("go_nogo", {})
    verdict = "GO" if all(gng.values()) else "NO-GO"
    pairs_s = ",".join(stats.get("by_pair", {}).keys())
    safe    = {k: v for k, v in stats.items()
               if k not in ("by_pair","by_session","by_day","by_regime",
                            "by_score","by_close_reason","by_month","go_nogo")}

    cur.execute("""INSERT INTO backtest_runs
        (run_time,pairs,start_balance,total_trades,win_rate,profit_factor,
         total_pnl,max_drawdown,sharpe,calmar,verdict,go_nogo,
         monte_carlo,stress_tests,full_stats)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        datetime.now(pytz.UTC).isoformat(), pairs_s,
        stats.get("total", 0),  # placeholder — start_balance not stored here
        stats.get("total", 0),
        stats.get("win_rate_pct", 0),
        stats.get("profit_factor", 0),
        stats.get("total_pnl", 0),
        stats.get("max_drawdown_pct", 0),
        stats.get("sharpe_approx", 0),
        stats.get("calmar_ratio", 0),
        verdict,
        json.dumps(gng),
        json.dumps(mc or {}),
        json.dumps(stress or {}),
        json.dumps(safe),
    ))
    run_id = cur.lastrowid

    for t in trades:
        cur.execute("""INSERT INTO backtest_trades
            (run_id,pair,direction,open_time,close_time,entry,actual_entry,
             sl,tp,lots,rr,sl_pips,tp_pips,session,day_of_week,hour_utc,
             regime,rsi,adx,macd_hist,atr,prefilter_score,spread_pips,
             drawdown_at_entry,outcome,close_reason,close_price,pips,pnl,
             balance_after,duration_mins,drawdown_at_close,trailing_used)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            run_id, t.get("pair"), t.get("direction"),
            str(t.get("open_time")), str(t.get("close_time")),
            t.get("entry"), t.get("actual_entry"),
            t.get("sl"), t.get("tp"), t.get("lots"), t.get("rr"),
            t.get("sl_pips"), t.get("tp_pips"), t.get("session"),
            t.get("day_of_week"), t.get("hour_utc"), t.get("regime"),
            t.get("rsi"), t.get("adx"), t.get("macd_hist"), t.get("atr"),
            t.get("prefilter_score"), t.get("spread_pips"),
            t.get("drawdown_at_entry"), t.get("outcome"), t.get("close_reason"),
            t.get("close_price"), t.get("pips"), t.get("pnl"),
            t.get("balance_after"), t.get("duration_mins"),
            t.get("drawdown_at_close"), int(t.get("trailing_used", False)),
        ))

    conn.commit(); conn.close()
    logger.info(f"SQLite saved: {db_path}  (run_id={run_id})")
    return run_id


def compare_runs(db_path: str, last_n: int = 5):
    if not os.path.exists(db_path):
        print(f"No database at {db_path}"); return
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, run_time, pairs, total_trades, win_rate,
                              profit_factor, total_pnl, max_drawdown, verdict
                       FROM backtest_runs ORDER BY id DESC LIMIT ?""", (last_n,))
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        print("No backtest runs found yet."); conn.close(); return
    finally:
        conn.close()
    if not rows: print("No runs found."); return
    print(f"\n{'='*80}")
    print(f"  LAST {last_n} BACKTEST RUNS")
    print(f"{'-'*80}")
    print(f"  {'ID':>4} {'Pairs':<18} {'Trades':>7} {'WR%':>6} "
          f"{'PF':>6} {'P&L':>10} {'MaxDD':>7} Verdict")
    print(f"{'-'*80}")
    for row in rows:
        rid, rt, pairs, trades, wr, pf, pnl, dd, verdict = row
        icon = "✓" if verdict == "GO" else "✗"
        print(f"  {rid:>4} {pairs:<18} {trades:>7} {wr:>5.1f}% "
              f"{pf:>6.3f} ${pnl:>+9.2f} {dd:>6.1f}%  {icon} {verdict}")
    print(f"{'='*80}\n")


# ============================================================
# CLI entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive forex backtester v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backtester.py                           All pairs, 6 months
  python backtester.py --pair EURUSD             Single pair only
  python backtester.py --months 3                3-month window
  python backtester.py --balance 500             $500 starting capital
  python backtester.py --no-session-filter       Include Asian session
  python backtester.py --trailing-sl             Enable trailing stop
  python backtester.py --stress                  Run 3 stress scenarios
  python backtester.py --monte-carlo             Monte Carlo (1000 runs)
  python backtester.py --spread-factor 1.5       Widen spread 1.5×
  python backtester.py --export trades.csv       Export trade log
  python backtester.py --optimize                Walk-forward optimization
  python backtester.py --verbose                 Print each trade live
  python backtester.py --compare-runs            Compare last 5 runs
  python backtester.py --csv-m15 m15.csv --csv-h1 h1.csv --pair EURUSD
        """
    )
    parser.add_argument("--pair",              nargs="+", default=None)
    parser.add_argument("--months",            type=int,   default=6)
    parser.add_argument("--balance",           type=float, default=490.70)
    parser.add_argument("--no-session-filter", action="store_true")
    parser.add_argument("--export",            type=str,   default=None)
    parser.add_argument("--db",                type=str,   default="data/backtest.db")
    parser.add_argument("--optimize",          action="store_true")
    parser.add_argument("--trailing-sl",       action="store_true")
    parser.add_argument("--stress",            action="store_true")
    parser.add_argument("--monte-carlo",       action="store_true")
    parser.add_argument("--spread-factor",     type=float, default=1.0)
    parser.add_argument("--verbose",           action="store_true")
    parser.add_argument("--compare-runs",      action="store_true")
    parser.add_argument("--csv-m15",           type=str,   default=None)
    parser.add_argument("--csv-h1",            type=str,   default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.compare_runs:
        compare_runs(args.db); return

    pairs         = args.pair or CONFIG["pairs"]
    apply_session = not args.no_session_filter

    print(f"\n{'='*66}")
    print(f"  FOREX BACKTESTER v2.0 — {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*66}")
    print(f"  Pairs:            {pairs}")
    print(f"  Period:           {args.months} months  (H1 fetches {args.months + H1_EXTRA_MONTHS}m)")
    print(f"  Start balance:    ${args.balance:,.2f}")
    print(f"  Session filter:   {'ON' if apply_session else 'OFF'}")
    print(f"  Min score:        {CONFIG['min_prefilter_score']}/9")
    print(f"  SL mult:          {SL_MULT}×ATR   TP mult: {TP_MULT}×ATR")
    print(f"  Trailing SL:      {'ON' if args.trailing_sl else 'OFF'}")
    print(f"  Spread factor:    {args.spread_factor}×")
    print(f"{'='*66}\n")

    os.makedirs("data", exist_ok=True)
    os.makedirs("backups", exist_ok=True)

    # ── Fetch data ─────────────────────────────────────────
    pairs_data = {}
    for pair in pairs:
        data = (load_csv_data(pair, args.csv_m15, args.csv_h1)
                if args.csv_m15 and args.csv_h1
                else fetch_mt5_data(pair, args.months))
        if data:
            print(f"  {pair}: {len(data['m15'])} M15 candles, {len(data['h1'])} H1 candles")
            if len(data["h1"]) < 250:
                print(f"  ⚠  {pair}: H1 only {len(data['h1'])} rows — "
                      f"need 250+ for EMA200. Try --months {args.months + 2}")
            pairs_data[pair] = data
        else:
            logger.warning(f"{pair}: data unavailable — skipped")

    if not pairs_data:
        print("\nERROR: No data loaded.")
        print("  MT5 mode: ensure terminal is open and .env credentials are correct")
        print("  CSV mode: use  --csv-m15 m15.csv --csv-h1 h1.csv --pair EURUSD")
        sys.exit(1)

    print()

    # ── Walk-forward mode ──────────────────────────────────
    if args.optimize:
        wf = walk_forward_optimize(
            pairs_data, args.balance,
            train_months=max(2, args.months // 3),
            test_months=max(1,  args.months // 6),
        )
        print(f"\n{'='*66}")
        print("  WALK-FORWARD RESULTS")
        print(f"{'-'*66}")
        print(f"  {'Train window':<24} {'Thr':>4} {'OOS WR%':>8} {'OOS PF':>7} "
              f"{'OOS P&L':>10} {'MaxDD%':>7}")
        print(f"{'-'*66}")
        for w in wf:
            print(f"  {w['train_start']}→{w['train_end']}  "
                  f"  {w['best_threshold']:>4}  "
                  f"{w['test_win_rate']:>7.1f}%  "
                  f"{w['test_pf']:>7.3f}  "
                  f"${w['test_pnl']:>+9.2f}  "
                  f"{w['test_max_dd']:>5.1f}%")
        print(f"{'='*66}\n")
        return

    # ── Full backtest ──────────────────────────────────────
    if len(pairs_data) == 1:
        pair   = list(pairs_data.keys())[0]
        result = backtest_pair(pair, pairs_data[pair], args.balance,
                               apply_session=apply_session, verbose=args.verbose,
                               use_trailing_sl=args.trailing_sl,
                               spread_factor=args.spread_factor)
    else:
        result = backtest_all_pairs(pairs_data, args.balance,
                                    apply_session=apply_session, verbose=args.verbose,
                                    use_trailing_sl=args.trailing_sl,
                                    spread_factor=args.spread_factor)

    trades = result.get("trades", [])
    if not trades:
        print("\nNo trades generated. Possible causes:")
        print("  • H1 data too short for EMA200 warmup (need 250+ H1 candles)")
        print("    → Try:  python backtester.py --months 8")
        print("  • All setups blocked by pre-filter or session rules")
        print("    → Try:  python backtester.py --no-session-filter")
        print("  • Data period too short (WARMUP_CANDLES=250 consumed too much)")
        sys.exit(0)

    print(f"  {len(trades)} simulated trades generated.\n")
    stats = compute_stats(trades, args.balance)

    # ── Monte Carlo ─────────────────────────────────────────
    mc = None
    if args.monte_carlo or len(trades) >= 50:
        print("  Running Monte Carlo (1000 runs)...")
        mc = monte_carlo(trades, args.balance)

    # ── Stress tests ────────────────────────────────────────
    stress = None
    if args.stress:
        print("  Running stress tests...")
        stress = run_stress_tests(pairs_data, args.balance, apply_session)

    print_report(stats, args.balance, mc=mc, stress=stress)

    if args.export:
        export_csv(trades, args.export)
        print(f"  CSV exported → {args.export}")

    run_id = export_sqlite(trades, stats, args.db, mc=mc, stress=stress)
    print(f"  Results saved → {args.db}  (run_id={run_id})")
    print(f"  Compare runs: python backtester.py --compare-runs")
    print(f"  SQLite query: sqlite3 {args.db} \"SELECT * FROM backtest_runs ORDER BY id DESC LIMIT 5\"\n")


if __name__ == "__main__":
    main()
