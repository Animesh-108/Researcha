# ============================================================
# main.py — Master loop connecting every module
#
# REQUIREMENTS BEFORE RUNNING:
#   1. MetaTrader 5 terminal open and logged into Exness demo
#   2. .env file filled with MT5 + Anthropic + Telegram keys
#   3. config.py: paper_trading = True (default, never change for demo)
#   4. pip install -r requirements.txt
#
# START COMMAND:
#   python main.py
#
# FOR 24/7 ON VPS:
#   screen -S trader
#   bash keep_alive.sh
# ============================================================

import os
import gc
import asyncio
import logging
import logging.handlers
import schedule
import psutil
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Logging — file + console before anything else
# ============================================================
from config import CONFIG

os.makedirs(os.path.dirname(CONFIG["log_path"]), exist_ok=True)
os.makedirs("data",    exist_ok=True)
os.makedirs("backups", exist_ok=True)

import sys
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.stream.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.handlers.RotatingFileHandler(
            CONFIG["log_path"],
            maxBytes=CONFIG["log_max_bytes"],
            backupCount=CONFIG["log_backup_count"],
            encoding="utf-8",
        ),
        _console_handler,
    ],
)
logger = logging.getLogger("main")

# ============================================================
# Imports
# ============================================================
from data_feed      import DataFeed
import indicators   as ind_module
from scanner        import run_prefilter
from news_filter    import is_news_blocked
from analyzer       import analyze
from executor       import Executor
from risk_manager   import RiskManager
from logger         import TradeLogger
import notifier
from trade_monitor  import TradeMonitor
from health_monitor import HealthMonitor
from weekly_review  import run_weekly_review

# ============================================================
# Global instances
# ============================================================
feed          = None
risk_manager  = None
executor      = None
trade_logger  = None
trade_monitor = None
health_mon    = None
_shutdown     = False


# ============================================================
# Startup checks
# ============================================================

def startup_checks():
    global feed, risk_manager, executor, trade_logger, trade_monitor, health_mon

    logger.info("=" * 60)
    logger.info("TRADING SYSTEM STARTING")
    mode = "PAPER TRADING" if CONFIG["paper_trading"] else "⚠️  LIVE TRADING"
    logger.info(f"Mode:   {mode}")
    logger.info(f"Pairs:  {CONFIG['pairs']}")
    logger.info(f"Signal: {CONFIG['signal_tf_label']} | Trend: {CONFIG['trend_tf_label']}")
    logger.info("=" * 60)

    # Check .env keys present
    for key in ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
                "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
        if not os.getenv(key):
            logger.critical(f"Missing required env var: {key}")
            return False

    # Connect MT5
    logger.info("Connecting to MT5...")
    feed = DataFeed()
    if not feed.is_connected():
        logger.critical(
            "MT5 connection failed.\n"
            "Check: MT5 terminal is open, logged in to Exness demo, and .env credentials are correct."
        )
        return False

    balance = feed.get_account_balance()
    if balance is None:
        logger.critical("Could not fetch account balance from MT5")
        return False
    logger.info(f"Account balance: ${balance:.2f}")

    # Init modules
    risk_manager  = RiskManager(starting_balance=balance)
    executor      = Executor(data_feed=feed)
    trade_logger  = TradeLogger()
    trade_monitor = TradeMonitor(feed, risk_manager, trade_logger, notifier)
    health_mon    = HealthMonitor(feed, notifier, trade_logger)

    # Wire into notifier for Telegram commands
    notifier.set_risk_manager(risk_manager)
    notifier.set_trade_logger(trade_logger)
    notifier.set_health_monitor(health_mon)

    # Restore today's trade count from database (survives restarts)
    today_stats = trade_logger.get_today_stats()
    closed_today = today_stats.get("total", 0)
    if closed_today > 0:
        risk_manager.trades_today = closed_today
        logger.info(f"Restored {closed_today} closed trades from today's database")

    # Load any already-open trades (after restart)
    open_trades = feed.get_open_trades()
    if open_trades:
        logger.warning(f"{len(open_trades)} open trade(s) already on MT5 at startup")
        for t in open_trades:
            risk_manager.add_open_trade(t)
            trade_monitor.register_trade(t["id"], None)  # db_id unknown after restart
            logger.info(f"  Registered: {t['pair']} {t['direction']} @ {t['entry']}")

    # Startup notification
    notifier.notify_system_event(
        "start",
        f"Balance: ${balance:.2f} | Mode: {mode} | Pairs: {', '.join(CONFIG['pairs'])}"
    )

    logger.info("All startup checks passed.")
    return True


# ============================================================
# Core scan — runs every 5 minutes
# ============================================================

async def scan_markets():
    """
    Full scan cycle for all pairs.
    Flow: risk check → news check → fetch data → indicators →
          pre-filter → Claude → Telegram → execute
    """
    if notifier.is_paused():
        return

    allowed, reason = risk_manager.can_trade()
    if not allowed:
        logger.info(f"Scan blocked: {reason}")
        return

    balance = feed.get_account_balance() or risk_manager.current_balance

    for pair in CONFIG["pairs"]:

        # Per-pair risk + correlation check
        pair_allowed, pair_reason = risk_manager.can_trade(pair=pair)
        if not pair_allowed:
            logger.debug(f"{pair} skipped: {pair_reason}")
            continue

        # News check
        news_blocked, news_reason = is_news_blocked(pair)
        if news_blocked:
            logger.info(f"{pair} news blocked: {news_reason}")
            trade_logger.log_signal(pair, None, 0, "NEUTRAL", None, None, news_blocked=True)
            continue

        # Fetch M15 candles
        df_signal = feed.get_candles(pair, timeframe=CONFIG["signal_timeframe"],
                                      count=CONFIG["candles_needed"])
        if df_signal is None:
            logger.warning(f"{pair}: M15 data unavailable")
            continue

        # Fetch H1 candles
        df_trend = feed.get_candles(pair, timeframe=CONFIG["trend_timeframe"], count=300)
        if df_trend is None:
            logger.warning(f"{pair}: H1 data unavailable")
            continue

        # Calculate indicators
        df_signal = ind_module.calculate_all(df_signal)
        df_trend  = ind_module.calculate_all(df_trend)

        if df_signal is None or df_trend is None:
            continue

        latest_signal = ind_module.get_latest(df_signal)
        latest_trend  = ind_module.get_latest(df_trend)

        if not latest_signal or not latest_trend:
            continue

        spread = feed.get_live_spread(pair)

        # Python pre-filter (free)
        passed, score, details, bias = run_prefilter(pair, latest_signal, spread)

        if not passed:
            logger.debug(f"{pair}: pre-filter {score}/9 — skip")
            trade_logger.log_signal(pair, None, score, bias, latest_signal, spread)
            del df_signal, df_trend
            gc.collect()
            continue

        logger.info(f"{pair}: pre-filter {score}/9 PASS (bias={bias}) — calling Claude")

        # Claude analysis — pass spread and pre-filter score for full context
        signal = analyze(pair, latest_signal, latest_trend, balance,
                         spread_pips=spread, prefilter_score=score)

        if signal is None or signal["signal"] == "NO TRADE":
            reason_txt = signal.get("reasoning", "")[:80] if signal else "Claude returned None"
            logger.info(f"{pair}: NO TRADE — {reason_txt}")
            trade_logger.log_signal(pair, signal, score, bias, latest_signal, spread)
            del df_signal, df_trend
            gc.collect()
            continue

        # Valid signal — log and send to Telegram
        logger.info(f"{pair}: {signal['signal']} | {signal['confidence']}% | R:R {signal.get('risk_reward')}")

        signal_id     = trade_logger.log_signal(
            pair, signal, score, bias, latest_signal, spread, sent_telegram=True
        )
        ind_snapshot  = latest_signal   # capture before cleanup

        # ------------------------------------------------
        # Callbacks for Telegram Accept/Reject
        # ------------------------------------------------
        async def on_accept(sig):
            trade_logger.update_signal_action(signal_id, "ACCEPTED")
            result = executor.place_trade(sig, risk_manager)

            if not result["success"]:
                logger.warning(f"{sig['pair']}: execution failed: {result['reason']}")
                notifier.send_message(f"⚠️ Execution failed ({sig['pair']}): {result['reason']}")
                return

            risk_amt = risk_manager.current_balance * CONFIG["risk_per_trade"]

            trade_db_id = trade_logger.log_trade_open(
                signal_id     = signal_id,
                pair          = sig["pair"],
                direction     = sig["signal"],
                paper         = result["paper"],
                order_id      = result["order_id"],
                fill_price    = result["fill_price"],
                stop_loss     = result["sl"],
                take_profit   = result["tp"],
                position_size = result["volume"],
                risk_amount   = risk_amt,
                signal_dict   = sig,
                ind           = ind_snapshot,
            )

            trade_entry = {
                "id":        result["order_id"],
                "pair":      sig["pair"],
                "direction": sig["signal"],
                "entry":     result["fill_price"],
                "sl":        result["sl"],
                "tp":        result["tp"],
                "volume":    result["volume"],
                "db_id":     trade_db_id,
                "unrealized": 0,
            }
            risk_manager.add_open_trade(trade_entry)

            # Register with trade monitor (live or paper)
            if result["paper"]:
                trade_monitor.register_paper_trade(
                    str(result["order_id"]), trade_db_id, trade_entry
                )
            else:
                trade_monitor.register_trade(result["order_id"], trade_db_id)

            notifier.notify_trade_opened(
                pair      = sig["pair"],
                direction = sig["signal"],
                fill_price = result["fill_price"],
                sl        = result["sl"],
                tp        = result["tp"],
                volume    = result["volume"],
                risk_amt  = risk_amt,
                paper     = result["paper"],
            )

        async def on_reject():
            trade_logger.update_signal_action(signal_id, "REJECTED")
            logger.info(f"{signal['pair']}: signal rejected by user")

        await notifier.send_signal_and_wait(
            signal=signal, pair=pair, balance=balance,
            on_accept=on_accept, on_reject=on_reject,
        )

        del df_signal, df_trend
        gc.collect()
        await asyncio.sleep(1)


# ============================================================
# Scheduled daily tasks
# ============================================================

def daily_tasks():
    logger.info("Running daily tasks")
    if trade_logger:
        trade_logger.backup_database()
    if risk_manager and trade_logger:
        stats = trade_logger.get_trade_stats(last_n=50)
        notifier.send_daily_summary(
            stats        = stats,
            balance      = risk_manager.current_balance,
            drawdown_pct = risk_manager.get_drawdown() * 100,
        )
    gc.collect()


def weekly_review_task():
    logger.info("Running weekly review")
    if trade_logger and risk_manager:
        review = run_weekly_review(trade_logger, risk_manager)
        if review:
            notifier.send_message("📊 *WEEKLY REVIEW*")
            for chunk in [review[i:i+3800] for i in range(0, len(review), 3800)]:
                notifier.send_message(chunk, parse_mode=None)


# ============================================================
# Schedule setup
# ============================================================

def setup_schedule():
    # scan_markets is async — handled directly in the asyncio loop below
    schedule.every().day.at("22:00").do(daily_tasks)
    schedule.every().sunday.at("20:00").do(weekly_review_task)
    logger.info(
        f"Scheduled: scan every {CONFIG['scan_interval_minutes']}min | "
        f"daily report 22:00 UTC | weekly review Sunday 20:00 UTC"
    )


# ============================================================
# Entry point
# ============================================================

def main():
    # Windows asyncio fix: ProactorEventLoop has known issues with stdin handles
    # (WinError 6 "The handle is invalid").  SelectorEventLoop is stable for
    # network-only asyncio apps and avoids the spurious CancelledError crashes.
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    logger.info("Initializing system...")

    if not startup_checks():
        logger.critical("Startup failed. Check errors above and fix before retrying.")
        return

    setup_schedule()

    # Start background threads
    trade_monitor.start()
    health_mon.start()

    # Build Telegram application
    tg_app = notifier.build_app()

    async def run_everything():
        # Register the running loop so background threads can post to Telegram safely
        notifier.set_main_loop(asyncio.get_event_loop())

        # Retry Telegram initialization — a single timeout should not crash the system
        tg_init_attempts = 0
        while True:
            try:
                await tg_app.initialize()
                break
            except Exception as e:
                tg_init_attempts += 1
                if tg_init_attempts >= 5:
                    logger.critical(f"Telegram failed to initialize after 5 attempts: {e}")
                    raise
                wait = 10 * tg_init_attempts
                logger.warning(f"Telegram init failed (attempt {tg_init_attempts}): {e} — retrying in {wait}s")
                await asyncio.sleep(wait)

        # initialize() already done above with retry — now just start
        try:
            await tg_app.start()
            await tg_app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started")
        except Exception as e:
            logger.critical(f"Telegram start failed: {e}")
            raise

        # Run first scan immediately on startup
        await scan_markets()

        scan_interval = CONFIG["scan_interval_minutes"] * 60
        last_scan     = asyncio.get_event_loop().time()

        try:
            while not _shutdown:
                schedule.run_pending()      # daily_tasks / weekly_review only

                now = asyncio.get_event_loop().time()
                if now - last_scan >= scan_interval:
                    last_scan = now
                    await scan_markets()

                await asyncio.sleep(10)

        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Main loop cancelled — shutting down gracefully")
        finally:
            try:
                await tg_app.updater.stop()
                await tg_app.stop()
                await tg_app.shutdown()
            except Exception as _e:
                logger.warning(f"Telegram shutdown warning (non-fatal): {_e}")

    try:
        asyncio.run(run_everything())
    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C)")
        notifier.notify_system_event("stop", "Manual shutdown")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        notifier.notify_system_event("error", str(e))
    finally:
        if trade_monitor:
            trade_monitor.stop()
        if health_mon:
            health_mon.stop()
        if feed:
            feed.shutdown()
        logger.info("System shut down cleanly.")


if __name__ == "__main__":
    main()