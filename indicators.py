# ============================================================
# indicators.py — Technical indicator calculation
# All calculated locally with pandas-ta. Zero API cost.
# ============================================================

import logging
import pandas as pd
import pandas_ta as ta
import numpy as np

logger = logging.getLogger(__name__)


def calculate_all(df):
    """
    Add all indicators to OHLCV dataframe.
    Returns enriched DataFrame or None on failure.

    Indicators:
        EMA 20/50/200, RSI 14, MACD 12/26/9,
        Bollinger Bands 20/2, ATR 14,
        Stochastic 14/3/3, ADX 14, Volume MA 20
    """
    if df is None or len(df) < 60:
        logger.warning("Insufficient candles for indicators")
        return None

    try:
        df = df.copy()

        # Trend — EMAs
        df["ema20"]  = ta.ema(df["close"], length=20)
        df["ema50"]  = ta.ema(df["close"], length=50)
        df["ema200"] = ta.ema(df["close"], length=200)
        df["ema20_slope"] = df["ema20"].diff(3)
        df["ema50_slope"] = df["ema50"].diff(3)

        # Momentum — RSI
        df["rsi"]      = ta.rsi(df["close"], length=14)
        df["rsi_prev"] = df["rsi"].shift(1)

        # Momentum — MACD
        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            df["macd"]           = macd["MACD_12_26_9"]
            df["macd_signal"]    = macd["MACDs_12_26_9"]
            df["macd_hist"]      = macd["MACDh_12_26_9"]
            df["macd_hist_prev"] = df["macd_hist"].shift(1)
        else:
            for col in ["macd", "macd_signal", "macd_hist", "macd_hist_prev"]:
                df[col] = np.nan

        # Volatility — Bollinger Bands (pandas-ta 0.4.x uses dynamic column names)
        bb = ta.bbands(df["close"], length=20, std=2)
        if bb is not None and not bb.empty:
            # Detect column names dynamically — 0.3.x uses BBU_20_2.0, 0.4.x may differ
            bbu = next((c for c in bb.columns if c.startswith("BBU_")), None)
            bbm = next((c for c in bb.columns if c.startswith("BBM_")), None)
            bbl = next((c for c in bb.columns if c.startswith("BBL_")), None)
            bbb = next((c for c in bb.columns if c.startswith("BBB_")), None)
            bbp = next((c for c in bb.columns if c.startswith("BBP_")), None)
            df["bb_upper"]  = bb[bbu] if bbu else np.nan
            df["bb_middle"] = bb[bbm] if bbm else np.nan
            df["bb_lower"]  = bb[bbl] if bbl else np.nan
            df["bb_width"]  = bb[bbb] if bbb else np.nan
            df["bb_pct"]    = bb[bbp] if bbp else np.nan
        else:
            for col in ["bb_upper", "bb_middle", "bb_lower", "bb_width", "bb_pct"]:
                df[col] = np.nan

        # Volatility — ATR (used for SL/TP sizing)
        df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

        # Momentum confirmation — Stochastic
        stoch = ta.stoch(df["high"], df["low"], df["close"], k=14, d=3, smooth_k=3)
        if stoch is not None and not stoch.empty:
            df["stoch_k"]      = stoch["STOCHk_14_3_3"]
            df["stoch_d"]      = stoch["STOCHd_14_3_3"]
            df["stoch_k_prev"] = df["stoch_k"].shift(1)
        else:
            for col in ["stoch_k", "stoch_d", "stoch_k_prev"]:
                df[col] = np.nan

        # Trend strength — ADX
        adx = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx is not None and not adx.empty:
            df["adx"] = adx["ADX_14"]
            df["dmp"] = adx["DMP_14"]
            df["dmn"] = adx["DMN_14"]
        else:
            for col in ["adx", "dmp", "dmn"]:
                df[col] = np.nan

        # Volume
        df["volume_ma"]    = ta.sma(df["volume"], length=20)
        df["volume_ratio"] = df["volume"] / df["volume_ma"]

        # Market regime derived from ADX + DI
        df["regime"] = "ranging"
        df.loc[(df["adx"] > 25) & (df["dmp"] > df["dmn"]), "regime"] = "trending_bull"
        df.loc[(df["adx"] > 25) & (df["dmn"] > df["dmp"]), "regime"] = "trending_bear"

        # Drop rows with NaN in critical columns
        df = df.dropna(subset=[
            "ema20", "ema50", "ema200",
            "rsi", "macd", "macd_hist",
            "bb_upper", "atr", "adx"
        ]).reset_index(drop=True)

        if len(df) < 10:
            logger.warning("Too few rows after NaN drop")
            return None

        return df

    except Exception as e:
        logger.error(f"Indicator calculation error: {e}", exc_info=True)
        return None


def get_latest(df):
    """
    Return latest candle values as a flat dict.
    Returns None if df is empty or None.
    """
    if df is None or df.empty:
        return None

    row  = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else row

    return {
        # Price
        "close":  row["close"],
        "open":   row["open"],
        "high":   row["high"],
        "low":    row["low"],
        "volume": row["volume"],
        # EMAs
        "ema20":           row["ema20"],
        "ema50":           row["ema50"],
        "ema200":          row["ema200"],
        "ema20_slope":     row["ema20_slope"],
        "ema50_slope":     row["ema50_slope"],
        # Price vs EMAs (bool flags)
        "above_ema20":     row["close"] > row["ema20"],
        "above_ema50":     row["close"] > row["ema50"],
        "above_ema200":    row["close"] > row["ema200"],
        "ema20_above_50":  row["ema20"] > row["ema50"],
        "ema50_above_200": row["ema50"] > row["ema200"],
        # RSI
        "rsi":         row["rsi"],
        "rsi_prev":    prev["rsi"],
        "rsi_rising":  row["rsi"] > prev["rsi"],
        # MACD
        "macd":             row["macd"],
        "macd_signal":      row["macd_signal"],
        "macd_hist":        row["macd_hist"],
        "macd_hist_prev":   prev["macd_hist"],
        "macd_hist_rising": row["macd_hist"] > prev["macd_hist"],
        "macd_bullish":     row["macd"] > row["macd_signal"],
        # Bollinger Bands
        "bb_upper":  row["bb_upper"],
        "bb_middle": row["bb_middle"],
        "bb_lower":  row["bb_lower"],
        "bb_width":  row["bb_width"],
        "bb_pct":    row["bb_pct"],
        # ATR
        "atr": row["atr"],
        # Stochastic
        "stoch_k":       row["stoch_k"],
        "stoch_d":       row["stoch_d"],
        "stoch_k_prev":  prev["stoch_k"],
        "stoch_bullish": row["stoch_k"] > row["stoch_d"],
        # ADX
        "adx": row["adx"],
        "dmp": row["dmp"],
        "dmn": row["dmn"],
        # Regime
        "regime":       row["regime"],
        # Volume
        "volume_ratio": row["volume_ratio"],
        # Time
        "candle_time":  row["time"],
    }
