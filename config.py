# ============================================================
# config.py — All system settings in one place
# Only change settings here. Never hardcode in other files.
# ============================================================

import MetaTrader5 as mt5

CONFIG = {
    # --------------------------------------------------------
    # PAIRS TO TRADE
    # Internal names — mapped to broker names via SYMBOL_MAP below
    # --------------------------------------------------------
    "pairs": ["EURUSD", "GBPUSD", "USDJPY"],

    # --------------------------------------------------------
    # TIMEFRAMES
    # --------------------------------------------------------
    "signal_timeframe": mt5.TIMEFRAME_M15,    # 15-min signals
    "trend_timeframe":  mt5.TIMEFRAME_H1,     # 1-hour trend
    "signal_tf_label":  "M15",
    "trend_tf_label":   "H1",
    "candles_needed":   300,

    # --------------------------------------------------------
    # TRADING SESSION (UTC)
    # London 07:00 | NY 13:00 | Close 21:00
    # --------------------------------------------------------
    "session_start_utc":  8,
    "session_end_utc":    21,
    "skip_weekends":      True,
    "skip_monday_before": 8,     # Skip Mon before 8am UTC
    "skip_friday_after":  20,    # Skip Fri after 8pm UTC

    # --------------------------------------------------------
    # SCAN SETTINGS
    # --------------------------------------------------------
    "scan_interval_minutes": 5,
    "paper_trading": True,       # TRUE = log only, no real orders
                                 # ALWAYS start True

    # --------------------------------------------------------
    # RISK MANAGEMENT — do not change these lightly
    # --------------------------------------------------------
    "risk_per_trade":           0.01,    # 1% risk per trade
    "max_daily_loss_pct":       0.03,    # Stop at 3% daily loss
    "max_consecutive_losses":   3,       # Pause after 3 in a row
    "max_trades_per_day":       10,
    "max_open_trades":          2,
    "min_risk_reward":          1.5,
    "sl_atr_multiplier":        1.5,     # SL = 1.5x ATR
    "tp_atr_multiplier":        2.5,     # TP = 2.5x ATR

    # --------------------------------------------------------
    # DRAWDOWN PROTECTION
    # --------------------------------------------------------
    "drawdown_reduce_size_pct": 0.05,    # Halve size at 5% DD
    "drawdown_pause_24h_pct":   0.10,    # Pause 24h at 10% DD
    "drawdown_pause_week_pct":  0.15,    # Pause 1 week at 15%
    "drawdown_lock_pct":        0.20,    # Full lock at 20%

    # --------------------------------------------------------
    # SIGNAL QUALITY FILTERS
    # --------------------------------------------------------
    "min_claude_confidence":  68,   # raised from 65 — require more conviction
    "min_prefilter_score":    7,   # raised from 6 — require stronger setups
    "max_spread_pips": {
        "EURUSD": 2.0,
        "GBPUSD": 2.5,
        "USDJPY": 2.0,
    },
    "min_atr": {
        "EURUSD": 0.0005,
        "GBPUSD": 0.0006,
        "USDJPY": 0.030,
    },
    "max_atr": {
        "EURUSD": 0.0030,
        "GBPUSD": 0.0035,
        "USDJPY": 0.180,
    },

    # --------------------------------------------------------
    # SIGNAL EXPIRY & SLIPPAGE
    # --------------------------------------------------------
    "signal_expiry_seconds": 180,
    "max_slippage_pips": {
        "EURUSD": 5,
        "GBPUSD": 5,
        "USDJPY": 5,
    },

    # --------------------------------------------------------
    # NEWS FILTER
    # --------------------------------------------------------
    "news_block_minutes_before": 30,
    "news_block_minutes_after":  15,
    "news_impact_levels":        ["High"],

    # --------------------------------------------------------
    # CORRELATION — pairs blocked from trading simultaneously
    # --------------------------------------------------------
    "correlated_pairs": {
        "EURUSD": ["GBPUSD"],
        "GBPUSD": ["EURUSD"],
        "USDJPY": [],
    },

    # --------------------------------------------------------
    # CLAUDE API
    # --------------------------------------------------------
    "claude_model":       "claude-sonnet-4-6",
    "claude_opus_model":  "claude-opus-4-6",
    "claude_max_tokens":  1500,
    "claude_timeout":     20,
    "claude_max_retries": 2,

    # --------------------------------------------------------
    # TRADE MONITOR
    # --------------------------------------------------------
    "trade_monitor_interval_secs": 30,

    # --------------------------------------------------------
    # HEALTH MONITOR
    # --------------------------------------------------------
    "health_check_minutes": 5,
    "max_memory_pct":       90,

    # --------------------------------------------------------
    # DATABASE & LOGS
    # --------------------------------------------------------
    "db_path":          "data/trades.db",
    "backup_path":      "backups/",
    "keep_backups":     7,
    "log_path":         "logs/system.log",
    "log_level":        "INFO",
    "log_max_bytes":    10_000_000,
    "log_backup_count": 5,

    # --------------------------------------------------------
    # MT5 MAGIC NUMBER
    # Unique ID to identify trades placed by this system
    # --------------------------------------------------------
    "mt5_magic": 20260508,
    "mt5_deviation": 20,    # Max slippage in points on order_send
}

# --------------------------------------------------------
# Symbol map — internal name → exact broker symbol name
# Check your MT5 Market Watch for exact names.
# Common Exness suffixes: none, m, .r
# --------------------------------------------------------
SYMBOL_MAP = {
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
}

# --------------------------------------------------------
# Pip size: 1 pip = this many price units
# For 5-digit brokers: EURUSD pip = 0.0001 = 10 × point
# --------------------------------------------------------
PIP_SIZE = {
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
}

# --------------------------------------------------------
# Currencies per pair (for news filter)
# --------------------------------------------------------
PAIR_CURRENCIES = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],

}