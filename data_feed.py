# ============================================================
# data_feed.py — Live market data from MetaTrader 5
# Requires MetaTrader 5 terminal to be running on Windows.
# Auto-reconnects on failure. Never raises to caller.
# ============================================================

import os
import time
import logging
import pandas as pd
from dotenv import load_dotenv
import MetaTrader5 as mt5
from config import CONFIG, SYMBOL_MAP, PIP_SIZE

load_dotenv()
logger = logging.getLogger(__name__)


class DataFeed:
    """
    Handles all MT5 data fetching.
    MT5 terminal must be open and logged in.
    All methods return None / empty list on failure.
    """

    def __init__(self):
        self._connected = False
        self._connect()

    def _connect(self):
        """Initialize MT5 connection."""
        login    = int(os.getenv("MT5_LOGIN", 0))
        password = os.getenv("MT5_PASSWORD", "")
        server   = os.getenv("MT5_SERVER", "")

        if not login or not password or not server:
            logger.critical("MT5_LOGIN, MT5_PASSWORD, MT5_SERVER must be set in .env")
            return False

        if not mt5.initialize(login=login, password=password, server=server):
            error = mt5.last_error()
            logger.error(f"MT5 initialize failed: {error}")
            self._connected = False
            return False

        info = mt5.account_info()
        if info is None:
            logger.error("MT5 connected but could not fetch account info")
            self._connected = False
            return False

        self._connected = True
        logger.info(
            f"MT5 connected | Account: {info.login} | "
            f"Balance: ${info.balance:.2f} | Server: {info.server}"
        )
        return True

    def _reconnect_with_backoff(self, max_attempts=5):
        """Exponential backoff reconnect."""
        for attempt in range(max_attempts):
            wait = 2 ** attempt
            logger.warning(f"MT5 reconnecting in {wait}s (attempt {attempt+1}/{max_attempts})")
            time.sleep(wait)
            mt5.shutdown()
            if self._connect():
                return True
        logger.error("All MT5 reconnection attempts failed")
        return False

    def _ensure_connected(self):
        """Check connection; reconnect if needed."""
        if not self._connected or mt5.account_info() is None:
            logger.warning("MT5 connection lost — reconnecting")
            return self._reconnect_with_backoff()
        return True

    def _broker_symbol(self, pair):
        """Map internal pair name to broker-specific symbol."""
        return SYMBOL_MAP.get(pair, pair)

    # --------------------------------------------------------
    # Candles
    # --------------------------------------------------------

    def get_candles(self, pair, timeframe=None, count=200):
        """
        Fetch OHLCV candles from MT5.
        Returns DataFrame(time, open, high, low, close, volume) or None.

        Args:
            pair:      internal pair name, e.g. "EURUSD"
            timeframe: mt5.TIMEFRAME_M15, mt5.TIMEFRAME_H1, etc.
            count:     number of candles to fetch
        """
        if not self._ensure_connected():
            return None

        if timeframe is None:
            timeframe = CONFIG["signal_timeframe"]

        symbol = self._broker_symbol(pair)

        for attempt in range(3):
            try:
                rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
                if rates is None or len(rates) == 0:
                    err = mt5.last_error()
                    logger.warning(f"{pair}: no candles returned — {err}")
                    if attempt < 2:
                        time.sleep(2)
                        self._ensure_connected()
                    continue

                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                df = df.rename(columns={"tick_volume": "volume"})[
                    ["time", "open", "high", "low", "close", "volume"]
                ]
                df = df.sort_values("time").reset_index(drop=True)

                if len(df) < 50:
                    logger.warning(f"{pair}: only {len(df)} candles returned")
                    return None

                logger.debug(f"{pair}: {len(df)} candles fetched")
                return df

            except Exception as e:
                logger.error(f"{pair} get_candles error (attempt {attempt+1}): {e}")
                if attempt < 2:
                    time.sleep(2)

        return None

    # --------------------------------------------------------
    # Spread
    # --------------------------------------------------------

    def get_live_spread(self, pair):
        """Returns current spread in pips or None."""
        if not self._ensure_connected():
            return None
        try:
            symbol = self._broker_symbol(pair)
            tick   = mt5.symbol_info_tick(symbol)
            if tick is None:
                return None
            spread_pts = tick.ask - tick.bid
            pip        = PIP_SIZE.get(pair, 0.0001)
            return round(spread_pts / pip, 1)
        except Exception as e:
            logger.error(f"{pair} spread error: {e}")
            return None

    # --------------------------------------------------------
    # Current price
    # --------------------------------------------------------

    def get_current_price(self, pair):
        """Returns current mid price or None."""
        if not self._ensure_connected():
            return None
        try:
            symbol = self._broker_symbol(pair)
            tick   = mt5.symbol_info_tick(symbol)
            if tick is None:
                return None
            return round((tick.bid + tick.ask) / 2, 5)
        except Exception as e:
            logger.error(f"{pair} price error: {e}")
            return None

    def get_ask(self, pair):
        """Returns current ask price (used for BUY orders)."""
        if not self._ensure_connected():
            return None
        try:
            symbol = self._broker_symbol(pair)
            tick   = mt5.symbol_info_tick(symbol)
            return tick.ask if tick else None
        except Exception as e:
            logger.error(f"{pair} ask error: {e}")
            return None

    def get_bid(self, pair):
        """Returns current bid price (used for SELL orders)."""
        if not self._ensure_connected():
            return None
        try:
            symbol = self._broker_symbol(pair)
            tick   = mt5.symbol_info_tick(symbol)
            return tick.bid if tick else None
        except Exception as e:
            logger.error(f"{pair} bid error: {e}")
            return None

    # --------------------------------------------------------
    # Account
    # --------------------------------------------------------

    def get_account_balance(self):
        """Returns current account balance or None."""
        if not self._ensure_connected():
            return None
        try:
            info = mt5.account_info()
            return float(info.balance) if info else None
        except Exception as e:
            logger.error(f"Balance fetch error: {e}")
            return None

    def get_account_info(self):
        """Returns full account info dict or None."""
        if not self._ensure_connected():
            return None
        try:
            info = mt5.account_info()
            if info is None:
                return None
            return {
                "login":    info.login,
                "balance":  info.balance,
                "equity":   info.equity,
                "margin":   info.margin,
                "free_margin": info.margin_free,
                "profit":   info.profit,
                "server":   info.server,
                "currency": info.currency,
            }
        except Exception as e:
            logger.error(f"Account info error: {e}")
            return None

    # --------------------------------------------------------
    # Open positions
    # --------------------------------------------------------

    def get_open_trades(self):
        """
        Returns list of open position dicts or empty list.
        Only returns positions opened by this system (magic number).
        """
        if not self._ensure_connected():
            return []
        try:
            positions = mt5.positions_get()
            if positions is None:
                return []
            magic  = CONFIG["mt5_magic"]
            # Reverse map: broker symbol → internal pair name
            reverse_map = {v: k for k, v in SYMBOL_MAP.items()}
            result = []
            for p in positions:
                if p.magic != magic:
                    continue
                internal_pair = reverse_map.get(p.symbol, p.symbol)
                result.append({
                    "id":         p.ticket,
                    "pair":       internal_pair,
                    "direction":  "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                    "volume":     p.volume,
                    "entry":      p.price_open,
                    "sl":         p.sl,
                    "tp":         p.tp,
                    "open_time":  p.time,
                    "unrealized": p.profit,
                    "comment":    p.comment,
                })
            return result
        except Exception as e:
            logger.error(f"Open positions error: {e}")
            return []

    # --------------------------------------------------------
    # Symbol info (for position sizing)
    # --------------------------------------------------------

    def get_symbol_info(self, pair):
        """Returns MT5 symbol info object or None."""
        if not self._ensure_connected():
            return None
        try:
            symbol = self._broker_symbol(pair)
            info   = mt5.symbol_info(symbol)
            if info is None:
                logger.error(f"Symbol info not found for {symbol}")
            return info
        except Exception as e:
            logger.error(f"Symbol info error: {e}")
            return None

    # --------------------------------------------------------
    # Health
    # --------------------------------------------------------

    def is_connected(self):
        """Quick connection health check."""
        return self._ensure_connected() and self.get_account_balance() is not None

    def shutdown(self):
        """Clean MT5 shutdown."""
        mt5.shutdown()
        self._connected = False
        logger.info("MT5 connection closed")
