# ============================================================
# risk_manager.py — All risk rules enforced here
# Most important file in the system.
# None of these rules should ever be bypassed.
# ============================================================

import logging
from datetime import datetime, timedelta
import pytz
from config import CONFIG, PIP_SIZE

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Tracks P&L, consecutive losses, open trades, drawdown.
    Enforces all hard stops automatically.
    """

    def __init__(self, starting_balance):
        self.starting_balance    = starting_balance
        self.current_balance     = starting_balance
        self.peak_balance        = starting_balance
        self.daily_start_balance = starting_balance
        self.daily_loss_pct      = 0.0
        self.consecutive_losses  = 0
        self.trades_today        = 0
        self.open_trades         = []
        self.daily_reset_date    = datetime.now(pytz.UTC).date()
        self.paused_until        = None
        self.locked              = False

    # --------------------------------------------------------
    # Balance tracking
    # --------------------------------------------------------

    def _reset_daily_if_needed(self):
        today = datetime.now(pytz.UTC).date()
        if today != self.daily_reset_date:
            logger.info(f"Daily reset. Previous day loss: {self.daily_loss_pct:.2%}")
            self.daily_start_balance = self.current_balance
            self.daily_loss_pct      = 0.0
            self.trades_today        = 0
            self.daily_reset_date    = today

    def update_balance(self, new_balance):
        """Call this after every trade closes."""
        self._reset_daily_if_needed()
        old_balance          = self.current_balance
        self.current_balance = new_balance

        if new_balance > self.peak_balance:
            self.peak_balance = new_balance

        daily_pnl = (new_balance - self.daily_start_balance) / self.daily_start_balance
        self.daily_loss_pct = abs(daily_pnl) if daily_pnl < 0 else 0.0

        if new_balance < old_balance:
            self.consecutive_losses += 1
            logger.warning(f"Loss. Consecutive losses: {self.consecutive_losses}")
        else:
            self.consecutive_losses = 0
            logger.info("Win. Consecutive loss counter reset.")

        self._check_drawdown_protection()

    def _check_drawdown_protection(self):
        dd = self.get_drawdown()

        if dd >= CONFIG["drawdown_lock_pct"]:
            self.locked = True
            logger.critical(f"SYSTEM LOCKED: {dd:.1%} drawdown hit {CONFIG['drawdown_lock_pct']:.0%}")
            return

        if dd >= CONFIG["drawdown_pause_week_pct"]:
            self.paused_until = datetime.now(pytz.UTC) + timedelta(days=7)
            logger.error(f"PAUSED 1 WEEK: {dd:.1%} drawdown. Resumes {self.paused_until.date()}")
            return

        if dd >= CONFIG["drawdown_pause_24h_pct"]:
            self.paused_until = datetime.now(pytz.UTC) + timedelta(hours=24)
            logger.warning(f"PAUSED 24H: {dd:.1%} drawdown. Resumes {self.paused_until}")
            return

    # --------------------------------------------------------
    # Position sizing
    # --------------------------------------------------------

    def get_position_size(self, pair, entry_price, stop_loss_price):
        """
        Risk exactly CONFIG['risk_per_trade'] % of balance.
        Reduced 50% during drawdown >= 5%.
        Returns units (int) or 0 if cannot calculate.
        """
        if not entry_price or not stop_loss_price:
            return 0

        pip_size     = PIP_SIZE.get(pair, 0.0001)
        pips_at_risk = abs(entry_price - stop_loss_price) / pip_size

        if pips_at_risk < 1:
            logger.warning(f"{pair}: SL too tight ({pips_at_risk:.1f} pips)")
            return 0

        risk_pct = CONFIG["risk_per_trade"]
        if self.get_drawdown() >= CONFIG["drawdown_reduce_size_pct"]:
            risk_pct *= 0.5
            logger.info(f"{pair}: halved position size due to drawdown")

        risk_amount   = self.current_balance * risk_pct
        pip_value     = pip_size                   # per unit, mid-price approximation
        position_size = risk_amount / (pips_at_risk * pip_value)
        position_size = int(position_size / 1000) * 1000  # round to nearest 1000 units
        position_size = max(1000, min(position_size, 100_000))

        logger.info(
            f"{pair} size: {position_size} units | "
            f"risk ${risk_amount:.2f} | pips {pips_at_risk:.1f}"
        )
        return position_size

    # --------------------------------------------------------
    # Master trade permission check
    # --------------------------------------------------------

    def can_trade(self, pair=None):
        """
        Returns (allowed: bool, reason: str).
        ALL checks must pass before any order is placed.
        """
        self._reset_daily_if_needed()

        if self.locked:
            return False, "SYSTEM LOCKED: 20% drawdown. Manual review required."

        if self.paused_until:
            now = datetime.now(pytz.UTC)
            if now < self.paused_until:
                remaining = self.paused_until - now
                hrs = remaining.seconds // 3600
                return False, f"System paused — resumes in {remaining.days}d {hrs}h"
            else:
                self.paused_until = None
                logger.info("Pause expired. Trading resumed.")

        in_session, reason = self._check_session()
        if not in_session:
            return False, reason

        if self.daily_loss_pct >= CONFIG["max_daily_loss_pct"]:
            return False, f"Daily loss limit: {self.daily_loss_pct:.2%} (max {CONFIG['max_daily_loss_pct']:.0%})"

        if self.consecutive_losses >= CONFIG["max_consecutive_losses"]:
            return False, f"Max consecutive losses ({self.consecutive_losses}). Take a break."

        if self.trades_today >= CONFIG["max_trades_per_day"]:
            return False, f"Max trades/day reached ({self.trades_today})"

        if len(self.open_trades) >= CONFIG["max_open_trades"]:
            return False, f"Max open trades ({len(self.open_trades)})"

        if pair:
            blocked, corr_reason = self._check_correlation(pair)
            if blocked:
                return False, corr_reason

        return True, "All checks passed"

    def _check_session(self):
        now     = datetime.now(pytz.UTC)
        hour    = now.hour
        weekday = now.weekday()

        if CONFIG["skip_weekends"] and weekday >= 5:
            return False, "Weekend — markets closed"
        if weekday == 0 and hour < CONFIG["skip_monday_before"]:
            return False, f"Monday before {CONFIG['skip_monday_before']}:00 UTC"
        if weekday == 4 and hour >= CONFIG["skip_friday_after"]:
            return False, f"Friday after {CONFIG['skip_friday_after']}:00 UTC"
        if hour < CONFIG["session_start_utc"] or hour >= CONFIG["session_end_utc"]:
            return False, f"Outside session ({CONFIG['session_start_utc']}:00-{CONFIG['session_end_utc']}:00 UTC)"
        return True, "In session"

    def _check_correlation(self, pair):
        blocked_with = CONFIG["correlated_pairs"].get(pair, [])
        open_pairs   = [t["pair"] for t in self.open_trades]
        for op in open_pairs:
            if op in blocked_with:
                return True, f"Correlated pair {op} already open"
        return False, "No correlation conflict"

    # --------------------------------------------------------
    # Open trade tracking
    # --------------------------------------------------------

    def add_open_trade(self, trade_dict):
        self.open_trades.append(trade_dict)
        self.trades_today += 1
        logger.info(f"Trade added: {trade_dict['pair']} {trade_dict['direction']}")

    def remove_open_trade(self, trade_id):
        self.open_trades = [t for t in self.open_trades if t.get("id") != trade_id]

    # --------------------------------------------------------
    # Metrics
    # --------------------------------------------------------

    def get_drawdown(self):
        if self.peak_balance == 0:
            return 0.0
        return (self.peak_balance - self.current_balance) / self.peak_balance

    def get_status(self):
        return {
            "balance":          round(self.current_balance, 2),
            "peak":             round(self.peak_balance, 2),
            "drawdown_pct":     round(self.get_drawdown() * 100, 2),
            "daily_loss_pct":   round(self.daily_loss_pct * 100, 2),
            "consecutive_loss": self.consecutive_losses,
            "trades_today":     self.trades_today,
            "open_trades":      len(self.open_trades),
            "locked":           self.locked,
            "paused_until":     str(self.paused_until) if self.paused_until else None,
        }
