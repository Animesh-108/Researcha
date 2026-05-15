# ============================================================
# logger.py — SQLite trade database
# WAL mode for crash safety. Daily backups.
# Logs every signal fired and every trade taken.
# ============================================================

import os
import shutil
import logging
from datetime import datetime, timedelta
import pytz
from sqlalchemy import (
    create_engine, Column, Integer, Float,
    String, DateTime, Boolean, Text, event
)
from sqlalchemy.orm import declarative_base, sessionmaker
from config import CONFIG

logger = logging.getLogger(__name__)
Base   = declarative_base()


# ============================================================
# Database models
# ============================================================

class Signal(Base):
    """Every signal that passes the pre-filter — whether taken or not."""
    __tablename__ = "signals"

    id              = Column(Integer, primary_key=True)
    timestamp       = Column(DateTime, default=lambda: datetime.now(pytz.UTC))
    pair            = Column(String(10))
    direction       = Column(String(10))      # BUY / SELL / NO TRADE
    confidence      = Column(Float)
    prefilter_score = Column(Integer)
    prefilter_bias  = Column(String(10))
    # M15 indicator snapshot
    rsi             = Column(Float)
    macd_hist       = Column(Float)
    ema20_slope     = Column(Float)
    adx             = Column(Float)
    atr             = Column(Float)
    bb_pct          = Column(Float)
    regime          = Column(String(20))
    spread_pips     = Column(Float)
    # Signal details
    entry_price     = Column(Float)
    stop_loss       = Column(Float)
    take_profit     = Column(Float)
    risk_reward     = Column(Float)
    risk_level      = Column(String(10))
    reasoning       = Column(Text)
    warning         = Column(Text)
    h1_bias         = Column(String(10))
    # Disposition
    sent_to_telegram = Column(Boolean, default=False)
    user_action      = Column(String(20))     # ACCEPTED / REJECTED / EXPIRED / AUTO
    news_blocked     = Column(Boolean, default=False)


class Trade(Base):
    """Every executed trade (paper or live)."""
    __tablename__ = "trades"

    id              = Column(Integer, primary_key=True)
    signal_id       = Column(Integer)
    timestamp       = Column(DateTime, default=lambda: datetime.now(pytz.UTC))
    pair            = Column(String(10))
    direction       = Column(String(10))
    paper           = Column(Boolean, default=True)
    order_id        = Column(String(50))
    fill_price      = Column(Float)
    stop_loss       = Column(Float)
    take_profit     = Column(Float)
    position_size   = Column(Float)
    risk_amount     = Column(Float)
    # Session context
    session         = Column(String(20))      # London / NY / Overlap
    day_of_week     = Column(String(10))
    hour_utc        = Column(Integer)
    # Outcome (filled when trade closes)
    close_price     = Column(Float)
    close_time      = Column(DateTime)
    outcome         = Column(String(10))      # WIN / LOSS / OPEN
    pnl             = Column(Float)
    pips            = Column(Float)
    balance_after   = Column(Float)
    duration_mins   = Column(Integer)
    # Claude analysis snapshot
    claude_confidence  = Column(Float)
    claude_reasoning   = Column(Text)
    rsi_at_entry       = Column(Float)
    macd_hist_at_entry = Column(Float)
    adx_at_entry       = Column(Float)
    regime_at_entry    = Column(String(20))


# ============================================================
# TradeLogger class
# ============================================================

class TradeLogger:
    def __init__(self):
        db_path = CONFIG["db_path"]
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        self.engine  = create_engine(f"sqlite:///{db_path}", echo=False)
        self.Session = sessionmaker(bind=self.engine)

        # Enable WAL mode — prevents database corruption on crash
        @event.listens_for(self.engine, "connect")
        def set_wal(dbapi_conn, _):
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA synchronous=NORMAL")

        Base.metadata.create_all(self.engine)
        logger.info(f"Database ready: {db_path}")

    # --------------------------------------------------------
    # Signal logging
    # --------------------------------------------------------

    def log_signal(self, pair, signal_dict, prefilter_score, prefilter_bias,
                   ind, spread_pips, sent_telegram=False, news_blocked=False):
        """Log a signal regardless of whether it was traded."""
        session = self.Session()
        try:
            rec = Signal(
                pair             = pair,
                direction        = signal_dict.get("signal", "NO TRADE") if signal_dict else "FILTERED",
                confidence       = signal_dict.get("confidence") if signal_dict else None,
                prefilter_score  = prefilter_score,
                prefilter_bias   = prefilter_bias,
                rsi              = ind.get("rsi") if ind else None,
                macd_hist        = ind.get("macd_hist") if ind else None,
                ema20_slope      = ind.get("ema20_slope") if ind else None,
                adx              = ind.get("adx") if ind else None,
                atr              = ind.get("atr") if ind else None,
                bb_pct           = ind.get("bb_pct") if ind else None,
                regime           = ind.get("regime") if ind else None,
                spread_pips      = spread_pips,
                entry_price      = signal_dict.get("entry_price") if signal_dict else None,
                stop_loss        = signal_dict.get("stop_loss") if signal_dict else None,
                take_profit      = signal_dict.get("take_profit") if signal_dict else None,
                risk_reward      = signal_dict.get("risk_reward") if signal_dict else None,
                risk_level       = signal_dict.get("risk_level") if signal_dict else None,
                reasoning        = signal_dict.get("reasoning") if signal_dict else None,
                warning          = signal_dict.get("warning") if signal_dict else None,
                h1_bias          = signal_dict.get("h1_bias") if signal_dict else None,
                sent_to_telegram = sent_telegram,
                news_blocked     = news_blocked,
            )
            session.add(rec)
            session.commit()
            return rec.id
        except Exception as e:
            session.rollback()
            logger.error(f"log_signal error: {e}")
            return None
        finally:
            session.close()

    def update_signal_action(self, signal_id, user_action):
        """Update what the user did with a signal."""
        if signal_id is None:
            return
        session = self.Session()
        try:
            rec = session.get(Signal, signal_id)
            if rec:
                rec.user_action = user_action
                session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"update_signal_action error: {e}")
        finally:
            session.close()

    # --------------------------------------------------------
    # Trade logging
    # --------------------------------------------------------

    def log_trade_open(self, signal_id, pair, direction, paper,
                       order_id, fill_price, stop_loss, take_profit,
                       position_size, risk_amount, signal_dict, ind):
        """Log a trade when it opens."""
        now = datetime.now(pytz.UTC)
        session = self.Session()
        try:
            rec = Trade(
                signal_id         = signal_id,
                pair              = pair,
                direction         = direction,
                paper             = paper,
                order_id          = str(order_id),
                fill_price        = fill_price,
                stop_loss         = stop_loss,
                take_profit       = take_profit,
                position_size     = position_size,
                risk_amount       = risk_amount,
                session           = _get_session_name(now),
                day_of_week       = now.strftime("%A"),
                hour_utc          = now.hour,
                outcome           = "OPEN",
                claude_confidence = signal_dict.get("confidence") if signal_dict else None,
                claude_reasoning  = signal_dict.get("reasoning") if signal_dict else None,
                rsi_at_entry      = ind.get("rsi") if ind else None,
                macd_hist_at_entry= ind.get("macd_hist") if ind else None,
                adx_at_entry      = ind.get("adx") if ind else None,
                regime_at_entry   = ind.get("regime") if ind else None,
            )
            session.add(rec)
            session.commit()
            logger.info(f"Trade logged: {direction} {pair} @ {fill_price}")
            return rec.id
        except Exception as e:
            session.rollback()
            logger.error(f"log_trade_open error: {e}")
            return None
        finally:
            session.close()

    def log_trade_close(self, trade_db_id, close_price, pnl, pips, balance_after):
        """Update trade record when it closes."""
        if trade_db_id is None:
            return
        close_time     = datetime.now(pytz.UTC)
        close_time_naive = close_time.replace(tzinfo=None)   # SQLite stores naive
        session        = self.Session()
        try:
            rec = session.get(Trade, trade_db_id)
            if rec:
                rec.close_price   = close_price
                rec.close_time    = close_time_naive
                rec.pnl           = pnl
                rec.pips          = pips
                rec.balance_after = balance_after
                rec.outcome       = "WIN" if (pnl or 0) > 0 else "LOSS"
                if rec.timestamp:
                    # Compare as naive UTC datetimes
                    open_naive = rec.timestamp.replace(tzinfo=None) if rec.timestamp.tzinfo else rec.timestamp
                    delta = (close_time_naive - open_naive).total_seconds() / 60
                    rec.duration_mins = int(delta)
                session.commit()
                logger.info(f"Trade {trade_db_id} closed: {'WIN' if pnl > 0 else 'LOSS'} ${pnl:.2f}")
        except Exception as e:
            session.rollback()
            logger.error(f"log_trade_close error: {e}")
        finally:
            session.close()

    # --------------------------------------------------------
    # Daily backup
    # --------------------------------------------------------

    def backup_database(self):
        """Copy database to backups folder. Keep last N days."""
        db_path     = CONFIG["db_path"]
        backup_path = CONFIG["backup_path"]
        keep_days   = CONFIG["keep_backups"]

        if not os.path.exists(db_path):
            return

        os.makedirs(backup_path, exist_ok=True)
        today    = datetime.now(pytz.UTC).strftime("%Y%m%d")
        dest     = os.path.join(backup_path, f"trades_{today}.db")

        try:
            shutil.copy2(db_path, dest)
            logger.info(f"Database backed up to {dest}")

            # Remove old backups
            backups = sorted([
                f for f in os.listdir(backup_path) if f.startswith("trades_")
            ])
            while len(backups) > keep_days:
                old = os.path.join(backup_path, backups.pop(0))
                os.remove(old)
                logger.info(f"Old backup removed: {old}")
        except Exception as e:
            logger.error(f"Backup error: {e}")

    # --------------------------------------------------------
    # Stats for weekly review / Telegram
    # --------------------------------------------------------

    def get_trade_stats(self, last_n=50):
        """Return basic stats dict from last N closed trades."""
        session = self.Session()
        try:
            from sqlalchemy import desc
            trades = (
                session.query(Trade)
                .filter(Trade.outcome.in_(["WIN", "LOSS"]))
                .order_by(desc(Trade.timestamp))
                .limit(last_n)
                .all()
            )
            if not trades:
                return {"total": 0}

            total = len(trades)
            wins  = sum(1 for t in trades if t.outcome == "WIN")
            pnl   = sum(t.pnl for t in trades if t.pnl is not None)

            return {
                "total":        total,
                "wins":         wins,
                "losses":       total - wins,
                "win_rate_pct": round(wins / total * 100, 1),
                "total_pnl":    round(pnl, 2),
                "avg_pnl":      round(pnl / total, 2),
            }
        except Exception as e:
            logger.error(f"get_trade_stats error: {e}")
            return {"total": 0}
        finally:
            session.close()

    def get_today_stats(self):
        """Return stats for today (UTC midnight to now)."""
        since = datetime.now(pytz.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self._get_stats_since(since)

    def get_week_stats(self):
        """Return stats for the last 7 days."""
        since = datetime.now(pytz.UTC) - timedelta(days=7)
        return self._get_stats_since(since)

    def get_month_stats(self):
        """Return stats for the last 30 days."""
        since = datetime.now(pytz.UTC) - timedelta(days=30)
        return self._get_stats_since(since)

    def _get_stats_since(self, since_dt):
        """Return stats for all closed trades since a given UTC datetime."""
        session = self.Session()
        empty   = {"total": 0, "wins": 0, "losses": 0,
                   "win_rate_pct": 0, "total_pnl": 0.0, "avg_pnl": 0.0}
        try:
            trades = (
                session.query(Trade)
                .filter(Trade.outcome.in_(["WIN", "LOSS"]))
                .filter(Trade.timestamp >= since_dt)
                .all()
            )
            if not trades:
                return empty

            total = len(trades)
            wins  = sum(1 for t in trades if t.outcome == "WIN")
            pnl   = sum(t.pnl for t in trades if t.pnl is not None)

            return {
                "total":        total,
                "wins":         wins,
                "losses":       total - wins,
                "win_rate_pct": round(wins / total * 100, 1),
                "total_pnl":    round(pnl, 2),
                "avg_pnl":      round(pnl / total, 2),
            }
        except Exception as e:
            logger.error(f"_get_stats_since error: {e}")
            return empty
        finally:
            session.close()

    def get_open_trades_count(self):
        """Return count of trades currently marked OPEN in the database."""
        session = self.Session()
        try:
            return session.query(Trade).filter(Trade.outcome == "OPEN").count()
        except Exception:
            return 0
        finally:
            session.close()

    def get_recent_trades_as_text(self, last_n=50):
        """Return recent closed trades as a plain text summary for Claude Opus review."""
        session = self.Session()
        try:
            from sqlalchemy import desc
            trades = (
                session.query(Trade)
                .filter(Trade.outcome.in_(["WIN", "LOSS"]))
                .order_by(desc(Trade.timestamp))
                .limit(last_n)
                .all()
            )
            lines = []
            for t in trades:
                lines.append(
                    f"{t.timestamp.strftime('%Y-%m-%d %H:%M')} | "
                    f"{t.pair} {t.direction} | "
                    f"confidence={t.claude_confidence} | "
                    f"regime={t.regime_at_entry} | "
                    f"session={t.session} | "
                    f"day={t.day_of_week} | "
                    f"rsi={t.rsi_at_entry} | "
                    f"adx={t.adx_at_entry} | "
                    f"outcome={t.outcome} | "
                    f"pnl=${t.pnl} | "
                    f"pips={t.pips}"
                )
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"get_recent_trades_as_text error: {e}")
            return ""
        finally:
            session.close()


# --------------------------------------------------------
# Helper
# --------------------------------------------------------

def _get_session_name(dt):
    hour = dt.hour
    if 7 <= hour < 12:
        return "London"
    elif 12 <= hour < 16:
        return "Overlap"
    elif 16 <= hour < 21:
        return "NY"
    else:
        return "Off-hours"
