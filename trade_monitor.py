# ============================================================
# trade_monitor.py — Watches open trades, detects closes
# Polls MT5 every 30 seconds for position changes.
# When a trade closes (SL/TP hit): updates balance, logs result,
# sends Telegram notification, removes from risk_manager.
# Runs in a background thread via main.py.
# ============================================================

import time
import logging
import threading
from datetime import datetime
import pytz
import MetaTrader5 as mt5
from config import CONFIG, PIP_SIZE

logger = logging.getLogger(__name__)


class TradeMonitor:
    """
    Polls MT5 every 30 seconds.
    Detects when open trades close (SL hit, TP hit, or manual close).
    Updates all downstream state: risk_manager, trade_logger, notifier.
    """

    def __init__(self, data_feed, risk_manager, trade_logger, notifier_module):
        self.feed         = data_feed
        self.rm           = risk_manager
        self.logger_db    = trade_logger
        self.notifier     = notifier_module
        self._running     = False
        self._thread      = None
        self._interval    = CONFIG["trade_monitor_interval_secs"]

        # Map: MT5 ticket → our internal trade_db_id
        # Populated when we open a trade, cleared when it closes
        self._ticket_to_db_id = {}

    def register_trade(self, mt5_ticket, trade_db_id):
        """Register an opened trade so monitor can match it on close."""
        self._ticket_to_db_id[str(mt5_ticket)] = trade_db_id
        logger.info(f"Trade registered for monitoring: ticket {mt5_ticket} → db_id {trade_db_id}")

    def start(self):
        """Start the monitor in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._monitor_loop,
            name="TradeMonitor",
            daemon=True,    # Exits automatically when main thread ends
        )
        self._thread.start()
        logger.info("Trade monitor started")

    def stop(self):
        """Signal the monitor thread to stop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Trade monitor stopped")

    # --------------------------------------------------------
    # Main loop
    # --------------------------------------------------------

    def _monitor_loop(self):
        """Runs every 30 seconds in background thread.
        Paper mode: check live prices against SL/TP locally.
        Live mode: poll MT5 for positions that disappeared (closed by broker).
        """
        while self._running:
            try:
                if CONFIG["paper_trading"]:
                    self.check_paper_trade_prices()   # never call MT5 history in paper mode
                else:
                    self._check_for_closed_trades()
            except Exception as e:
                logger.error(f"Trade monitor error: {e}", exc_info=True)
            time.sleep(self._interval)

    def _check_for_closed_trades(self):
        """
        Compare our registered tickets against currently open MT5 positions.
        Any ticket that was open before but is now gone has closed.
        """
        if not self._ticket_to_db_id:
            return  # Nothing to check

        # Get currently open tickets
        open_positions   = self.feed.get_open_trades()
        open_tickets_now = {str(p["id"]) for p in open_positions}

        # Find tickets that were open before but aren't now
        our_tickets = list(self._ticket_to_db_id.keys())
        for ticket_str in our_tickets:
            if ticket_str not in open_tickets_now:
                self._handle_closed_trade(ticket_str)

    def _handle_closed_trade(self, ticket_str):
        """
        Called when a previously open trade is no longer in MT5 positions.
        Fetches the closed deal from MT5 history and processes the result.
        """
        trade_db_id = self._ticket_to_db_id.pop(ticket_str, None)

        # Find this trade in risk_manager's open trades to get entry details
        trade_info = None
        for t in self.rm.open_trades:
            if str(t.get("id")) == ticket_str:
                trade_info = t
                break

        if trade_info:
            self.rm.remove_open_trade(trade_info.get("id"))

        # Get closed deal details from MT5 history
        close_price, pnl, pips = self._fetch_close_details(ticket_str, trade_info)

        # Update balance
        new_balance = self.feed.get_account_balance()
        if new_balance:
            self.rm.update_balance(new_balance)

        # Always log closure — use 0 fallbacks if data unavailable
        if trade_db_id:
            self.logger_db.log_trade_close(
                trade_db_id   = trade_db_id,
                close_price   = close_price or 0,
                pnl           = pnl or 0,
                pips          = pips or 0,
                balance_after = new_balance or self.rm.current_balance,
            )

        # Send Telegram notification
        if trade_info:
            outcome = "WIN" if (pnl or 0) > 0 else "LOSS"
            self.notifier.notify_trade_closed(
                pair          = trade_info.get("pair", "?"),
                direction     = trade_info.get("direction", "?"),
                entry         = trade_info.get("entry", 0),
                close_price   = close_price or 0,
                pnl           = pnl or 0,
                pips          = pips or 0,
                balance_after = new_balance or self.rm.current_balance,
                outcome       = outcome,
                paper         = CONFIG["paper_trading"],
            )

        logger.info(
            f"Trade closed: ticket {ticket_str} | "
            f"P&L ${pnl:.2f} | pips {pips:.1f}" if pnl else
            f"Trade closed: ticket {ticket_str}"
        )

    def _fetch_close_details(self, ticket_str, trade_info):
        """
        Fetch closing price and P&L from MT5 deal history.
        Returns (close_price, pnl, pips) or (None, None, None) on failure.
        """
        # Paper trades have no MT5 history — skip straight to fallback
        if str(ticket_str).startswith("PAPER-"):
            if trade_info:
                return None, trade_info.get("unrealized", 0), 0
            return None, None, None

        try:
            ticket = int(ticket_str)
            # Search last 60 seconds of history
            from_time = int(datetime.now(pytz.UTC).timestamp()) - 60
            to_time   = int(datetime.now(pytz.UTC).timestamp()) + 10

            deals = mt5.history_deals_get(from_time, to_time)
            if deals is None:
                deals = []

            # Find the deal that closed this ticket
            for deal in deals:
                if deal.position_id == ticket and deal.entry == mt5.DEAL_ENTRY_OUT:
                    close_price = deal.price
                    pnl         = deal.profit

                    # Calculate pips
                    pips = 0
                    if trade_info and trade_info.get("entry") and close_price:
                        pair     = trade_info.get("pair", "EURUSD")
                        pip_size = PIP_SIZE.get(pair, 0.0001)
                        raw_diff = close_price - trade_info["entry"]
                        if trade_info.get("direction") == "SELL":
                            raw_diff = -raw_diff
                        pips = raw_diff / pip_size

                    return close_price, pnl, round(pips, 1)

        except Exception as e:
            logger.error(f"fetch_close_details error for ticket {ticket_str}: {e}")

        # Fallback: estimate from trade_info
        if trade_info:
            pnl = trade_info.get("unrealized", 0)
            return None, pnl, 0

        return None, None, None

    # --------------------------------------------------------
    # Paper trade tracking (for paper mode)
    # --------------------------------------------------------

    def register_paper_trade(self, paper_order_id, trade_db_id, trade_info):
        """
        In paper mode, MT5 has no real ticket.
        We still track paper trades in our local state.
        Paper trades are closed by the user via Telegram or automatically
        when SL/TP prices are hit by the current price feed.
        """
        self._ticket_to_db_id[paper_order_id] = trade_db_id
        logger.info(f"Paper trade registered: {paper_order_id} → db_id {trade_db_id}")

    def check_paper_trade_prices(self):
        """
        For paper trading: check if any open paper trades have hit SL or TP.
        Called from monitor loop when paper_trading = True.
        """
        for t in list(self.rm.open_trades):
            if not str(t.get("id", "")).startswith("PAPER-"):
                continue

            pair  = t.get("pair")
            price = self.feed.get_current_price(pair)
            if price is None:
                continue

            direction = t.get("direction")
            sl        = t.get("sl")
            tp        = t.get("tp")
            entry     = t.get("entry", 0)

            hit_sl = hit_tp = False
            if direction == "BUY":
                hit_sl = sl and price <= sl
                hit_tp = tp and price >= tp
            else:
                hit_sl = sl and price >= sl
                hit_tp = tp and price <= tp

            if hit_sl or hit_tp:
                close_price = sl if hit_sl else tp
                volume      = t.get("volume", 0.01)
                pip_size    = PIP_SIZE.get(pair, 0.0001)

                # Pips gained/lost
                raw_diff = close_price - entry
                if direction == "SELL":
                    raw_diff = -raw_diff
                pips = raw_diff / pip_size

                # Correct forex P&L for USD account
                # USDJPY: profit in JPY → convert to USD at close price
                # All others (EURUSD, GBPUSD): second currency is USD already
                if pair == "USDJPY":
                    pnl = raw_diff * volume * 100000 / close_price
                else:
                    pnl = raw_diff * volume * 100000

                pnl = round(pnl, 2)

                trade_db_id = self._ticket_to_db_id.get(str(t["id"]))
                new_balance = self.rm.current_balance + pnl
                self.rm.update_balance(new_balance)
                self.rm.remove_open_trade(t["id"])
                self._ticket_to_db_id.pop(str(t["id"]), None)

                if trade_db_id:
                    self.logger_db.log_trade_close(
                        trade_db_id   = trade_db_id,
                        close_price   = close_price,
                        pnl           = pnl,
                        pips          = round(pips, 1),
                        balance_after = new_balance,
                    )

                outcome = "WIN" if pnl > 0 else "LOSS"
                reason  = "TP hit" if hit_tp else "SL hit"
                self.notifier.notify_trade_closed(
                    pair          = pair,
                    direction     = direction,
                    entry         = entry,
                    close_price   = close_price,
                    pnl           = round(pnl, 2),
                    pips          = round(pips, 1),
                    balance_after = round(new_balance, 2),
                    outcome       = outcome,
                    paper         = True,
                )
                logger.info(f"[PAPER] {pair} closed ({reason}): {outcome} ${pnl:.2f}")
