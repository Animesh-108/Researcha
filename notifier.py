# ============================================================
# notifier.py — Telegram bot
# Signal alerts with ACCEPT / REJECT buttons + 3-min expiry.
# Full command set: /status /balance /today /week /month
#                   /pause /resume /stop /review /health
#                   /settings /trades /history /help
# ============================================================

import os
import asyncio
import logging
from datetime import datetime, timedelta
import pytz
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from dotenv import load_dotenv
from config import CONFIG

load_dotenv()
logger = logging.getLogger(__name__)

# ============================================================
# Shared state
# ============================================================
_pending_signals   = {}    # msg_id → {pair, signal, event, response}
_system_paused     = False
_risk_manager_ref  = None
_trade_logger_ref  = None
_health_monitor_ref = None


def set_risk_manager(rm):
    global _risk_manager_ref
    _risk_manager_ref = rm

def set_trade_logger(tl):
    global _trade_logger_ref
    _trade_logger_ref = tl

def set_health_monitor(hm):
    global _health_monitor_ref
    _health_monitor_ref = hm


# ============================================================
# Formatting
# ============================================================

def _format_signal(signal, pair, balance):
    direction = signal["signal"]
    emoji     = "🟢" if direction == "BUY" else "🔴"
    risk_amt  = balance * CONFIG["risk_per_trade"]
    conf      = signal["confidence"]
    conf_bar  = "█" * (conf // 10) + "░" * (10 - conf // 10)

    display_pair = pair.replace("USD", "/USD").replace("EUR/", "EUR/").replace("GBP/", "GBP/").replace("EURUSD", "EUR/USD").replace("GBPUSD", "GBP/USD").replace("USDJPY", "USD/JPY")

    lines = [
        f"{emoji} *{direction} SIGNAL — {display_pair}*",
        f"",
        f"Confidence:    `{conf}%`  [{conf_bar}]",
        f"Entry:         `{signal.get('entry_price', 'market')}`",
        f"Stop Loss:     `{signal.get('stop_loss')}`",
        f"Take Profit:   `{signal.get('take_profit')}`",
        f"Risk/Reward:   `{signal.get('risk_reward', '?'):.2f}`" if signal.get("risk_reward") else f"Risk/Reward:   `{signal.get('risk_reward', '?')}`",
        f"Risk Amount:   `${risk_amt:.2f}`",
        f"Risk Level:    `{signal.get('risk_level', 'MEDIUM')}`",
        f"H1 Bias:       `{signal.get('h1_bias', 'N/A')}`",
        f"",
        f"*Key confluences:*",
    ]
    for kc in (signal.get("key_confluences") or [])[:3]:
        lines.append(f"▸ {kc}")
    lines += [
        f"",
        f"*Reasoning:*",
        f"{signal.get('reasoning', 'No reasoning')}",
    ]
    if signal.get("warning"):
        lines += ["", f"⚠️ _{signal['warning']}_"]
    lines += ["", "⏱ _Signal expires in 3 minutes_"]
    return "\n".join(lines)


# ============================================================
# Core async send
# ============================================================

async def _send_async(text, parse_mode="Markdown", reply_markup=None):
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    bot     = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode=parse_mode, reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")
        return None


_main_loop = None

def set_main_loop(loop):
    """Called from main.py to register the running event loop."""
    global _main_loop
    _main_loop = loop

def send_message(text, parse_mode="Markdown"):
    """Thread-safe fire-and-forget Telegram message.
    Works from both async context and background threads."""
    if _main_loop and _main_loop.is_running():
        # Called from a background thread — schedule on the main event loop
        asyncio.run_coroutine_threadsafe(_send_async(text, parse_mode), _main_loop)
    else:
        # Called before loop started or after it stopped
        try:
            asyncio.run(_send_async(text, parse_mode))
        except Exception as e:
            logger.error(f"send_message fallback error: {e}")


# ============================================================
# Signal with Accept/Reject
# ============================================================

async def send_signal_and_wait(signal, pair, balance, on_accept, on_reject=None):
    """
    Send signal alert with buttons. Wait up to 3 minutes.
    Calls on_accept(signal) or on_reject().
    Returns action: "ACCEPTED" / "REJECTED" / "EXPIRED" / "ERROR"
    """
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    bot     = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
    expiry  = CONFIG["signal_expiry_seconds"]

    display_pair = pair.replace("EURUSD","EUR/USD").replace("GBPUSD","GBP/USD").replace("USDJPY","USD/JPY")

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ACCEPT", callback_data=f"accept|{pair}"),
        InlineKeyboardButton("❌ REJECT", callback_data=f"reject|{pair}"),
    ]])

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=_format_signal(signal, pair, balance),
            parse_mode="Markdown",
            reply_markup=markup,
        )
    except Exception as e:
        logger.error(f"Signal send error: {e}")
        return "ERROR"

    msg_id          = msg.message_id
    response_event  = asyncio.Event()
    response_store  = {"action": None}

    _pending_signals[msg_id] = {
        "pair": pair, "signal": signal,
        "event": response_event, "response": response_store,
    }

    try:
        await asyncio.wait_for(response_event.wait(), timeout=expiry)
        action = response_store["action"]
    except asyncio.TimeoutError:
        action = "EXPIRED"
        _pending_signals.pop(msg_id, None)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏱ *EXPIRED* — {display_pair} signal cancelled (no response in 3 min)",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if action == "ACCEPTED" and on_accept:
        await on_accept(signal)
    elif action in ("REJECTED", "EXPIRED") and on_reject:
        await on_reject()

    return action


# ============================================================
# Trade notifications
# ============================================================

def notify_trade_opened(pair, direction, fill_price, sl, tp, volume, risk_amt, paper):
    display = pair.replace("EURUSD","EUR/USD").replace("GBPUSD","GBP/USD").replace("USDJPY","USD/JPY")
    emoji   = "🟢" if direction == "BUY" else "🔴"
    mode    = "📋 PAPER" if paper else "💰 LIVE"
    send_message(
        f"{emoji} *TRADE OPENED* [{mode}]\n\n"
        f"Pair:     `{display}`\n"
        f"Direction: `{direction}`\n"
        f"Entry:    `{fill_price:.5f}`\n"
        f"SL:       `{sl:.5f}`\n"
        f"TP:       `{tp:.5f}`\n"
        f"Volume:   `{volume} lots`\n"
        f"Risk:     `${risk_amt:.2f}`"
    )


def notify_trade_closed(pair, direction, entry, close_price, pnl, pips, balance_after, outcome, paper):
    display   = pair.replace("EURUSD","EUR/USD").replace("GBPUSD","GBP/USD").replace("USDJPY","USD/JPY")
    emoji     = "✅" if outcome == "WIN" else "❌"
    mode      = "📋 PAPER" if paper else "💰 LIVE"
    exit_str  = f"{close_price:.5f}" if close_price else "N/A"
    send_message(
        f"{emoji} *TRADE CLOSED* [{mode}]\n\n"
        f"Pair:    `{display}`\n"
        f"Result:  `{outcome}`\n"
        f"Entry:   `{entry:.5f}`\n"
        f"Exit:    `{exit_str}`\n"
        f"Pips:    `{pips:+.1f}`\n"
        f"P&L:     `${pnl:+.2f}`\n"
        f"Balance: `${balance_after:.2f}`"
    )


def notify_system_event(event_type, detail=""):
    icons = {"start": "✅", "stop": "🛑", "pause": "⏸", "resume": "▶️", "error": "⚠️", "locked": "🔒"}
    icon  = icons.get(event_type, "ℹ️")
    send_message(f"{icon} *System {event_type.upper()}*\n{detail}")


def send_daily_summary(stats, balance, drawdown_pct):
    send_message(
        f"📊 *DAILY SUMMARY*\n\n"
        f"Trades:   `{stats.get('total', 0)}`\n"
        f"Wins:     `{stats.get('wins', 0)}`\n"
        f"Losses:   `{stats.get('losses', 0)}`\n"
        f"Win Rate: `{stats.get('win_rate_pct', 0)}%`\n"
        f"P&L:      `${stats.get('total_pnl', 0):+.2f}`\n"
        f"Balance:  `${balance:.2f}`\n"
        f"Drawdown: `{drawdown_pct:.1f}%`"
    )


# ============================================================
# Telegram Application
# ============================================================

def build_app():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    app   = Application.builder().token(token).build()
    app.add_handler(CallbackQueryHandler(_handle_callback))
    for cmd, fn in [
        ("status",   _cmd_status),
        ("balance",  _cmd_balance),
        ("today",    _cmd_today),
        ("week",     _cmd_week),
        ("month",    _cmd_month),
        ("pause",    _cmd_pause),
        ("resume",   _cmd_resume),
        ("stop",     _cmd_stop),
        ("review",   _cmd_review),
        ("health",   _cmd_health),
        ("settings", _cmd_settings),
        ("trades",   _cmd_trades),
        ("history",  _cmd_history),
        ("help",     _cmd_help),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    return app


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    data   = query.data
    action, pair = data.split("|", 1)
    msg_id = query.message.message_id

    pending = _pending_signals.pop(msg_id, None)
    if pending is None:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    user_action = "ACCEPTED" if action == "accept" else "REJECTED"
    pending["response"]["action"] = user_action
    pending["event"].set()

    display = pair.replace("EURUSD","EUR/USD").replace("GBPUSD","GBP/USD").replace("USDJPY","USD/JPY")
    icon    = "✅" if user_action == "ACCEPTED" else "❌"
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"{icon} {user_action} — {display}",
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rm = _risk_manager_ref
    if rm is None:
        await update.message.reply_text("System not running.")
        return
    s    = rm.get_status()
    mode = "📋 PAPER" if CONFIG["paper_trading"] else "💰 LIVE"
    state = "⏸ PAUSED" if _system_paused else "▶️ RUNNING"
    await update.message.reply_text(
        f"🖥 *SYSTEM STATUS* [{mode}]\n\n"
        f"State:      `{state}`\n"
        f"Balance:    `${s['balance']}`\n"
        f"Drawdown:   `{s['drawdown_pct']}%`\n"
        f"Daily Loss: `{s['daily_loss_pct']}%`\n"
        f"Consec L:   `{s['consecutive_loss']}`\n"
        f"Trades/day: `{s['trades_today']}`\n"
        f"Open:       `{s['open_trades']}`\n"
        f"Locked:     `{s['locked']}`",
        parse_mode="Markdown"
    )


async def _cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rm = _risk_manager_ref
    if rm:
        await update.message.reply_text(f"💰 Balance: `${rm.current_balance:.2f}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("System not running.")


async def _cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tl = _trade_logger_ref
    rm = _risk_manager_ref
    if not tl:
        await update.message.reply_text("System not running.")
        return
    stats      = tl.get_today_stats()
    open_count = len(rm.open_trades) if rm else 0
    await update.message.reply_text(
        f"📅 *TODAY*\n\n"
        f"Closed:   `{stats.get('total', 0)}`\n"
        f"Open now: `{open_count}`\n"
        f"Wins:     `{stats.get('wins', 0)}`\n"
        f"Losses:   `{stats.get('losses', 0)}`\n"
        f"Win Rate: `{stats.get('win_rate_pct', 0)}%`\n"
        f"P&L:      `${stats.get('total_pnl', 0):+.2f}`",
        parse_mode="Markdown"
    )


async def _cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tl = _trade_logger_ref
    if not tl:
        await update.message.reply_text("System not running.")
        return
    stats = tl.get_week_stats()
    await update.message.reply_text(
        f"📅 *LAST 7 DAYS*\n\n"
        f"Trades:    `{stats.get('total', 0)}`\n"
        f"Wins:      `{stats.get('wins', 0)}`\n"
        f"Losses:    `{stats.get('losses', 0)}`\n"
        f"Win Rate:  `{stats.get('win_rate_pct', 0)}%`\n"
        f"Total P&L: `${stats.get('total_pnl', 0):+.2f}`\n"
        f"Avg P&L:   `${stats.get('avg_pnl', 0):+.2f}`",
        parse_mode="Markdown"
    )


async def _cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tl = _trade_logger_ref
    if not tl:
        await update.message.reply_text("System not running.")
        return
    stats = tl.get_month_stats()
    await update.message.reply_text(
        f"📅 *LAST 30 DAYS*\n\n"
        f"Trades:    `{stats.get('total', 0)}`\n"
        f"Wins:      `{stats.get('wins', 0)}`\n"
        f"Losses:    `{stats.get('losses', 0)}`\n"
        f"Win Rate:  `{stats.get('win_rate_pct', 0)}%`\n"
        f"Total P&L: `${stats.get('total_pnl', 0):+.2f}`\n"
        f"Avg P&L:   `${stats.get('avg_pnl', 0):+.2f}`",
        parse_mode="Markdown"
    )


async def _cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _system_paused
    _system_paused = True
    await update.message.reply_text("⏸ *System paused* — no new signals.\nType /resume to restart.", parse_mode="Markdown")
    logger.warning("System paused via Telegram")


async def _cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _system_paused
    _system_paused = False
    await update.message.reply_text("▶️ *System resumed* — scanning markets.", parse_mode="Markdown")
    logger.info("System resumed via Telegram")


async def _cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _system_paused
    _system_paused = True
    await update.message.reply_text(
        "🛑 *Emergency stop triggered*\n\n"
        "No new signals will fire.\n"
        "⚠️ Open trades are NOT automatically closed — check MT5.",
        parse_mode="Markdown"
    )
    logger.critical("Emergency stop via Telegram")


async def _cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Running weekly review now... (may take 30 seconds)")
    from weekly_review import run_weekly_review
    tl = _trade_logger_ref
    rm = _risk_manager_ref
    if tl and rm:
        review = run_weekly_review(tl, rm)
        if review:
            chunks = [review[i:i+3800] for i in range(0, len(review), 3800)]
            for chunk in chunks:
                await update.message.reply_text(chunk)
        else:
            await update.message.reply_text("Review failed or not enough trades.")
    else:
        await update.message.reply_text("System not running.")


async def _cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hm = _health_monitor_ref
    if hm is None:
        await update.message.reply_text("Health monitor not running.")
        return
    s = hm.get_status_dict()
    await update.message.reply_text(
        f"🔍 *HEALTH CHECK*\n\n"
        f"MT5:      {'✅' if s['mt5'] else '❌'}\n"
        f"Claude:   {'✅' if s['claude'] else '❌'}\n"
        f"Telegram: {'✅' if s['telegram'] else '❌'}\n"
        f"Database: {'✅' if s['database'] else '❌'}\n"
        f"RAM:      `{s['ram_pct']}%`\n"
        f"Disk:     `{s['disk_pct']}%`",
        parse_mode="Markdown"
    )


async def _cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "PAPER" if CONFIG["paper_trading"] else "LIVE"
    await update.message.reply_text(
        f"⚙️ *CURRENT SETTINGS*\n\n"
        f"Mode:         `{mode}`\n"
        f"Pairs:        `{', '.join(CONFIG['pairs'])}`\n"
        f"Timeframe:    `{CONFIG['signal_tf_label']} signal / {CONFIG['trend_tf_label']} trend`\n"
        f"Session:      `{CONFIG['session_start_utc']}:00–{CONFIG['session_end_utc']}:00 UTC`\n"
        f"Risk/trade:   `{CONFIG['risk_per_trade']*100:.0f}%`\n"
        f"Max daily L:  `{CONFIG['max_daily_loss_pct']*100:.0f}%`\n"
        f"Min conf:     `{CONFIG['min_claude_confidence']}%`\n"
        f"Min R:R:      `{CONFIG['min_risk_reward']}`\n"
        f"Scan every:   `{CONFIG['scan_interval_minutes']} min`",
        parse_mode="Markdown"
    )


async def _cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rm = _risk_manager_ref
    if not rm:
        await update.message.reply_text("System not running.")
        return
    trades = rm.open_trades
    if not trades:
        await update.message.reply_text("No open trades.")
        return
    lines = ["📂 *OPEN TRADES*\n"]
    for t in trades:
        display = str(t.get("pair","?")).replace("EURUSD","EUR/USD").replace("GBPUSD","GBP/USD").replace("USDJPY","USD/JPY")
        lines.append(
            f"{display} {t.get('direction','?')} | "
            f"entry {t.get('entry','?')} | "
            f"unrealized ${t.get('unrealized',0):.2f}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tl = _trade_logger_ref
    if not tl:
        await update.message.reply_text("System not running.")
        return
    text = tl.get_recent_trades_as_text(last_n=10)
    if not text:
        await update.message.reply_text("No completed trades yet.")
        return
    await update.message.reply_text(f"📜 *LAST 10 TRADES*\n\n```\n{text}\n```", parse_mode="Markdown")


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *COMMANDS*\n\n"
        "/status   — System overview\n"
        "/balance  — Current balance\n"
        "/today    — Today's P&L\n"
        "/week     — Week's performance\n"
        "/month    — Month's performance\n"
        "/trades   — Open trades\n"
        "/history  — Last 10 trades\n"
        "/pause    — Pause signals\n"
        "/resume   — Resume signals\n"
        "/stop     — Emergency stop\n"
        "/review   — Run weekly AI review now\n"
        "/health   — System health check\n"
        "/settings — View current config\n"
        "/help     — This message",
        parse_mode="Markdown"
    )


def is_paused():
    return _system_paused
