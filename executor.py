# ============================================================
# executor.py — MT5 trade placement
# Checks slippage before execution.
# Calculates lot size from 1% risk rule.
# Paper trading mode: logs trade but places NO real order.
# SL and TP always attached to order on MT5 server.
# ============================================================

import logging
from datetime import datetime
import pytz
import MetaTrader5 as mt5
from config import CONFIG, SYMBOL_MAP, PIP_SIZE

logger = logging.getLogger(__name__)


class Executor:
    """Handles trade execution on MT5."""

    def __init__(self, data_feed):
        self.feed  = data_feed
        self.paper = CONFIG["paper_trading"]

        if self.paper:
            logger.info("Executor: PAPER TRADING mode — no real orders")
        else:
            logger.warning("Executor: LIVE mode — real orders will be placed")

    # --------------------------------------------------------
    # Public: place trade
    # --------------------------------------------------------

    def place_trade(self, signal, risk_manager):
        """
        Execute a trade from a Claude signal.

        Args:
            signal:       dict from analyzer.analyze()
            risk_manager: RiskManager instance (for balance + position sizing)

        Returns:
            dict with: success, order_id, fill_price, sl, tp,
                       volume, paper, pair, direction, reason
        """
        pair      = signal["pair"]
        direction = signal["signal"]    # "BUY" or "SELL"
        symbol    = SYMBOL_MAP.get(pair, pair)

        if direction not in ("BUY", "SELL"):
            return {"success": False, "reason": f"Invalid direction: {direction}"}

        # ------------------------------------------------
        # Slippage check
        # ------------------------------------------------
        slippage_ok, slippage_reason, exec_price = self._check_slippage(
            pair, signal.get("entry_price"), direction
        )
        if not slippage_ok:
            logger.warning(f"{pair}: trade rejected — {slippage_reason}")
            return {"success": False, "reason": slippage_reason}

        # Adjust SL/TP from new execution price if slipped
        sl, tp = self._adjust_sl_tp(pair, signal, exec_price, direction)

        # Enforce broker stop-distance + spread-safe SL/TP
        ok, sl, tp, reason = self._enforce_stop_distance(pair, direction, exec_price, sl, tp)
        if not ok:
            logger.warning(f"{pair}: trade rejected — {reason}")
            return {"success": False, "reason": reason}

        # ------------------------------------------------
        # Position sizing
        # ------------------------------------------------
        volume = self._calculate_lot_size(pair, exec_price, sl, risk_manager)
        if volume <= 0:
            return {"success": False, "reason": "Could not calculate valid lot size"}

        # ------------------------------------------------
        # Paper trading
        # ------------------------------------------------
        if self.paper:
            order_id = f"PAPER-{datetime.now(pytz.UTC).strftime('%Y%m%d%H%M%S')}"
            logger.info(
                f"[PAPER] {direction} {pair} | {volume} lots | "
                f"entry ~{exec_price:.5f} | SL {sl:.5f} | TP {tp:.5f}"
            )
            return {
                "success":   True,
                "paper":     True,
                "order_id":  order_id,
                "fill_price": exec_price,
                "sl":        sl,
                "tp":        tp,
                "volume":    volume,
                "pair":      pair,
                "direction": direction,
                "reason":    "Paper trade",
            }

        # ------------------------------------------------
        # Live MT5 execution
        # ------------------------------------------------
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    volume,
            "type":      order_type,
            "price":     exec_price,
            "sl":        sl,
            "tp":        tp,
            "deviation": CONFIG["mt5_deviation"],
            "magic":     CONFIG["mt5_magic"],
            "comment":   "TradingBot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

        if result is None:
            err = mt5.last_error()
            logger.error(f"{pair}: order_send returned None | error: {err}")
            return {"success": False, "reason": f"MT5 error: {err}"}

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(
                f"Order filled: {direction} {pair} | "
                f"{volume} lots | price {result.price} | deal {result.deal}"
            )
            return {
                "success":    True,
                "paper":      False,
                "order_id":   result.deal,
                "fill_price": result.price,
                "sl":         sl,
                "tp":         tp,
                "volume":     volume,
                "pair":       pair,
                "direction":  direction,
                "reason":     "Order filled",
            }
        else:
            reason = self._retcode_description(result.retcode)
            logger.warning(f"{pair}: order rejected — retcode {result.retcode}: {reason}")
            return {"success": False, "reason": f"MT5 rejected: {reason}"}

    # --------------------------------------------------------
    # Close trade
    # --------------------------------------------------------

    def close_trade(self, trade_id, pair, volume, direction):
        """
        Close a specific open trade by ticket.
        In paper mode, just logs.
        """
        if self.paper:
            logger.info(f"[PAPER] Closed trade {trade_id} on {pair}")
            return {"success": True, "paper": True}

        symbol   = SYMBOL_MAP.get(pair, pair)
        close_type = mt5.ORDER_TYPE_SELL if direction == "BUY" else mt5.ORDER_TYPE_BUY
        tick     = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"success": False, "reason": "Could not fetch tick for close"}

        close_price = tick.bid if direction == "BUY" else tick.ask

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    symbol,
            "volume":    volume,
            "type":      close_type,
            "position":  trade_id,
            "price":     close_price,
            "deviation": CONFIG["mt5_deviation"],
            "magic":     CONFIG["mt5_magic"],
            "comment":   "TradingBot-Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"Trade {trade_id} closed on {pair}")
            return {"success": True, "paper": False, "close_price": result.price}
        else:
            retcode = result.retcode if result else "None"
            reason  = self._retcode_description(retcode) if result else "no result"
            logger.error(f"Close trade {trade_id} failed: {reason}")
            return {"success": False, "reason": reason}

    # --------------------------------------------------------
    # Slippage check
    # --------------------------------------------------------

    def _check_slippage(self, pair, signal_entry, direction):
        """
        Checks if current price is still close enough to signal entry.
        Returns (ok, reason, exec_price)
        """
        if direction == "BUY":
            exec_price = self.feed.get_ask(pair)
        else:
            exec_price = self.feed.get_bid(pair)

        if exec_price is None:
            logger.warning(f"{pair}: price unavailable for slippage check — proceeding at signal price")
            return True, "Price unavailable — using signal price", signal_entry or 0

        if signal_entry is None:
            return True, "No signal entry to check", exec_price

        pip_size    = PIP_SIZE.get(pair, 0.0001)
        slippage    = abs(exec_price - signal_entry) / pip_size
        max_slip    = CONFIG["max_slippage_pips"].get(pair, 5)

        if slippage <= max_slip:
            return True, f"Slippage {slippage:.1f} pips OK", exec_price
        else:
            return False, f"Slippage {slippage:.1f} pips > max {max_slip} pips", exec_price

    # --------------------------------------------------------
    # Adjust SL/TP if price slipped
    # --------------------------------------------------------

    def _adjust_sl_tp(self, pair, signal, exec_price, direction):
        """
        Keep SL and TP at same pip distance from new execution price.
        """
        original_entry = signal.get("entry_price") or exec_price
        sl_orig = signal.get("stop_loss")
        tp_orig = signal.get("take_profit")

        if sl_orig is None or tp_orig is None:
            return sl_orig, tp_orig

        sl_distance = abs(original_entry - sl_orig)
        tp_distance = abs(original_entry - tp_orig)

        if direction == "BUY":
            sl = exec_price - sl_distance
            tp = exec_price + tp_distance
        else:
            sl = exec_price + sl_distance
            tp = exec_price - tp_distance

        digits = self._get_digits(pair)
        return round(sl, digits), round(tp, digits)

    # --------------------------------------------------------
    # Broker stop-distance & spread-safe SL/TP
    # --------------------------------------------------------

    def _enforce_stop_distance(self, pair, direction, exec_price, sl, tp):
        if sl is None or tp is None:
            return False, sl, tp, "SL/TP missing"

        info = self.feed.get_symbol_info(pair)
        digits = info.digits if info else 5
        point  = info.point if info else (10 ** -digits)
        stops_level = info.trade_stops_level if info else 0
        min_distance = stops_level * point if stops_level else 0.0

        # Ensure SL/TP on correct side of entry
        if direction == "BUY" and (sl >= exec_price or tp <= exec_price):
            return False, sl, tp, "SL/TP not on correct side for BUY"
        if direction == "SELL" and (sl <= exec_price or tp >= exec_price):
            return False, sl, tp, "SL/TP not on correct side for SELL"

        # Enforce minimum broker distance from entry
        if min_distance > 0:
            if direction == "BUY":
                sl = min(sl, exec_price - min_distance)
                tp = max(tp, exec_price + min_distance)
            else:
                sl = max(sl, exec_price + min_distance)
                tp = min(tp, exec_price - min_distance)

        # Ensure SL/TP not inside current spread
        bid = self.feed.get_bid(pair)
        ask = self.feed.get_ask(pair)
        if bid and ask:
            if direction == "BUY":
                if sl >= bid:
                    sl = bid - (min_distance or point)
                if tp <= ask:
                    tp = ask + (min_distance or point)
            else:
                if sl <= ask:
                    sl = ask + (min_distance or point)
                if tp >= bid:
                    tp = bid - (min_distance or point)

        sl = round(sl, digits)
        tp = round(tp, digits)

        # Final sanity
        if direction == "BUY" and (sl >= exec_price or tp <= exec_price):
            return False, sl, tp, "SL/TP invalid after stop-distance enforcement"
        if direction == "SELL" and (sl <= exec_price or tp >= exec_price):
            return False, sl, tp, "SL/TP invalid after stop-distance enforcement"

        return True, sl, tp, "OK"

    def _get_digits(self, pair):
        info = self.feed.get_symbol_info(pair)
        return info.digits if info else 5

    # --------------------------------------------------------
    # Position sizing
    # --------------------------------------------------------

    def _calculate_lot_size(self, pair, entry_price, stop_loss, risk_manager):
        """
        Risk exactly 1% of balance (adjusted if in drawdown).
        Returns lot size or 0 on failure.

        MT5 pip value approach:
        - symbol_info().trade_tick_value = USD value of 1 point per 1 lot
        - pip_value_per_lot = tick_value × 10 (since 1 pip = 10 points for 5-digit)
        - lot_size = risk_amount / (pip_risk × pip_value_per_lot)
        """
        pip  = PIP_SIZE.get(pair, 0.0001)
        if stop_loss is None or entry_price is None:
            return 0

        pip_risk = abs(entry_price - stop_loss) / pip
        if pip_risk < 1:
            logger.warning(f"{pair}: SL too tight ({pip_risk:.1f} pips)")
            return 0

        # Apply drawdown reduction if needed
        from config import CONFIG
        risk_pct = CONFIG["risk_per_trade"]
        if risk_manager.get_drawdown() >= CONFIG["drawdown_reduce_size_pct"]:
            risk_pct *= 0.5
            logger.info(f"{pair}: halved position size due to drawdown")

        risk_amount = risk_manager.current_balance * risk_pct

        # Get pip value from MT5
        sym_info = self.feed.get_symbol_info(pair)
        if sym_info:
            # trade_tick_value = value of 1 point per 1 lot in account currency
            pip_value_per_lot = sym_info.trade_tick_value * 10   # 10 points per pip
            if pip_value_per_lot <= 0:
                pip_value_per_lot = 10.0  # fallback: ~$10/pip for major pairs per lot
        else:
            pip_value_per_lot = 10.0  # fallback

        lot_size = risk_amount / (pip_risk * pip_value_per_lot)

        # Round to lot step
        if sym_info:
            step     = sym_info.volume_step   # e.g. 0.01
            lot_min  = sym_info.volume_min    # e.g. 0.01
            lot_max  = sym_info.volume_max    # e.g. 100.0
        else:
            step, lot_min, lot_max = 0.01, 0.01, 10.0

        lot_size = round(round(lot_size / step) * step, 2)
        lot_size = max(lot_min, min(lot_size, lot_max))

        logger.info(
            f"{pair}: {lot_size} lots | risk ${risk_amount:.2f} | "
            f"pip_risk {pip_risk:.1f} | pip_val ${pip_value_per_lot:.2f}/lot"
        )
        return lot_size

    # --------------------------------------------------------
    # MT5 retcode descriptions
    # --------------------------------------------------------

    def _retcode_description(self, retcode):
        descriptions = {
            mt5.TRADE_RETCODE_REQUOTE:          "Requote",
            mt5.TRADE_RETCODE_REJECT:           "Request rejected",
            mt5.TRADE_RETCODE_CANCEL:           "Request cancelled",
            mt5.TRADE_RETCODE_PLACED:           "Order placed",
            mt5.TRADE_RETCODE_DONE:             "Done",
            mt5.TRADE_RETCODE_DONE_PARTIAL:     "Done partial",
            mt5.TRADE_RETCODE_ERROR:            "Error",
            mt5.TRADE_RETCODE_TIMEOUT:          "Timeout",
            mt5.TRADE_RETCODE_INVALID:          "Invalid request",
            mt5.TRADE_RETCODE_INVALID_VOLUME:   "Invalid volume",
            mt5.TRADE_RETCODE_INVALID_PRICE:    "Invalid price",
            mt5.TRADE_RETCODE_INVALID_STOPS:    "Invalid stops",
            mt5.TRADE_RETCODE_TRADE_DISABLED:   "Trade disabled",
            mt5.TRADE_RETCODE_MARKET_CLOSED:    "Market closed",
            mt5.TRADE_RETCODE_NO_MONEY:         "Insufficient funds",
            mt5.TRADE_RETCODE_PRICE_CHANGED:    "Price changed",
            mt5.TRADE_RETCODE_PRICE_OFF:        "No price quotes",
            mt5.TRADE_RETCODE_INVALID_EXPIRATION: "Invalid expiry",
            mt5.TRADE_RETCODE_LOCKED:           "Locked",
            mt5.TRADE_RETCODE_FROZEN:           "Frozen",
            mt5.TRADE_RETCODE_INVALID_FILL:     "Invalid fill",
            mt5.TRADE_RETCODE_CONNECTION:       "No connection",
            mt5.TRADE_RETCODE_TOO_MANY_REQUESTS: "Too many requests",
        }
        return descriptions.get(retcode, f"Unknown retcode {retcode}")
