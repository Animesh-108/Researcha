# ============================================================
# backtester.py — Comprehensive Historical Backtester v2.1
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
#   - Claude-proxy decision layer (confidence + confluence filter)
#
# DOES NOT SIMULATE:
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
MIN_CONF       = CONFIG["min_claude_confidence"]
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

# (unchanged)

# ============================================================
# Session check — mirrors risk_manager.py exactly
# ============================================================

# (unchanged)

# ============================================================
# SL/TP calculation — mirrors analyzer.py exactly
# ============================================================

# (unchanged)

# ============================================================
# Position sizing — mirrors executor.py exactly
# ============================================================

# (unchanged)

# ============================================================
# P&L calculation
# ============================================================

# (unchanged)

# ============================================================
# Helpers
# ============================================================

# (unchanged)

# ============================================================
# Claude-proxy decision layer
# ============================================================


def _claude_proxy(pair: str, direction: str, signal_ind: dict, trend_ind: dict) -> tuple:
    """
    Deterministic proxy for Claude decisions.
    Returns (allow_trade: bool, confidence: int, confluences: list, reason: str)
    """
    confluences = []

    # H1 trend strength gate (Claude rule)
    if trend_ind.get("adx", 0) < 20:
        return False, 0, confluences, "H1 ADX < 20 (ranging)"

    # EMA stack alignment on M15
    ema_bull = signal_ind.get("ema20_above_50") and signal_ind.get("ema50_above_200")
    ema_bear = (not signal_ind.get("ema20_above_50")) and (not signal_ind.get("ema50_above_200"))
    if direction == "BUY" and ema_bull:
        confluences.append("EMA stack bullish")
    if direction == "SELL" and ema_bear:
        confluences.append("EMA stack bearish")

    # MACD momentum
    if direction == "BUY" and signal_ind.get("macd_hist_rising") and signal_ind.get("macd_bullish"):
        confluences.append("MACD momentum bullish")
    if direction == "SELL" and (not signal_ind.get("macd_bullish")) and (not signal_ind.get("macd_hist_rising")):
        confluences.append("MACD momentum bearish")

    # RSI momentum
    if direction == "BUY" and signal_ind.get("rsi_rising") and signal_ind.get("rsi", 50) > 50:
        confluences.append("RSI rising above 50")
    if direction == "SELL" and (not signal_ind.get("rsi_rising")) and signal_ind.get("rsi", 50) < 50:
        confluences.append("RSI falling below 50")

    # BB position + expansion
    bb_pct = signal_ind.get("bb_pct")
    bb_width = signal_ind.get("bb_width")
    if bb_pct is not None and bb_width is not None:
        if direction == "BUY" and bb_pct > 0.80 and bb_width > 0.8:
            confluences.append("BB upper expansion")
        if direction == "SELL" and bb_pct < 0.20 and bb_width > 0.8:
            confluences.append("BB lower expansion")

    # ADX strength on M15
    if signal_ind.get("adx", 0) > 25:
        confluences.append("ADX strong")

    # Volume conviction
    if signal_ind.get("volume_ratio", 1.0) >= 1.2:
        confluences.append("Volume above avg")

    # Confidence scoring (simple deterministic proxy)
    confidence = min(95, 50 + len(confluences) * 8)

    if len(confluences) < 3:
        return False, confidence, confluences, "<3 confluences"
    if confidence < MIN_CONF:
        return False, confidence, confluences, f"confidence {confidence} < {MIN_CONF}"

    return True, confidence, confluences, "OK"


# ============================================================
# Trailing SL
# ============================================================

# (unchanged)

# ============================================================
# Open-trade record builder (shared by both engines)
# ============================================================


def _build_trade_record(pair, direction, ct, entry, actual_entry, sl, tp,
                         sl_p, tp_p, rr, lots, balance, atr, signal_ind,
                         score, sim_spr, dd, confidence=None, confluences=None):
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
        "confidence":        confidence,
        "confluences":       confluences or [],
        "trailing_active":   False,
        "best_price":        actual_entry,
    }


# ============================================================
# Single-pair backtest engine
# ============================================================

# (unchanged until entry block below)

# NOTE: replaced in both engines where direction is resolved


# ============================================================
# Multi-pair engine with correlation enforcement
# ============================================================

# (unchanged until entry block below)


# ============================================================
# Statistics engine — comprehensive
# ============================================================

# (unchanged)

# ============================================================
# Walk-forward optimizer
# ============================================================

# (unchanged)

# ============================================================
# Monte Carlo simulation
# ============================================================

# (unchanged)

# ============================================================
# Stress tests
# ============================================================

# (unchanged)

# ============================================================
# Report printer
# ============================================================

# (unchanged)

# ============================================================
# Exports
# ============================================================

# (unchanged)

# ============================================================
# CLI entry point
# ============================================================

# (unchanged)
