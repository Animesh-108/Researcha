# ============================================================
# weekly_review.py — Claude Opus weekly performance analysis
# Runs every Sunday. Analyzes last 50 closed trades.
# Identifies patterns, gives blunt feedback, suggests changes.
# ============================================================

import os
import logging
import anthropic
from dotenv import load_dotenv
from config import CONFIG

load_dotenv()
logger = logging.getLogger(__name__)


def run_weekly_review(trade_logger, risk_manager):
    """
    Pull last 50 trades, send to Claude Opus for honest review.
    Returns review text or None on failure.
    """
    trades_text = trade_logger.get_recent_trades_as_text(last_n=50)
    stats       = trade_logger.get_trade_stats(last_n=50)
    rm_status   = risk_manager.get_status()

    if stats.get("total", 0) < 5:
        msg = "Not enough trades for weekly review (need at least 5)."
        logger.info(msg)
        return msg

    prompt = f"""You are reviewing the performance of an automated forex trading system.
Analyze the last {stats['total']} trades honestly and give actionable feedback.

SYSTEM PERFORMANCE SUMMARY:
Total trades: {stats['total']}
Wins:         {stats['wins']}
Losses:       {stats['losses']}
Win Rate:     {stats['win_rate_pct']}%
Total P&L:    ${stats['total_pnl']}
Account:      ${rm_status['balance']}
Drawdown:     {rm_status['drawdown_pct']}%

TRADE LOG (most recent first):
{trades_text}

Analyze this data and give a ruthlessly honest review. Address:
1. Where is the system performing well? (best pairs, sessions, regimes, confidence ranges)
2. Where is it failing? (patterns in losing trades — pair, time, regime, indicator conditions)
3. Is the win rate trend improving, stable, or deteriorating?
4. Specific adjustments recommended (e.g. "remove GBP/USD", "only trade London overlap", "raise confidence threshold to 70%")
5. Is this system viable as-is, needs refinement, or needs rebuilding?

Be direct. No sugarcoating. This trader needs real feedback to improve."""

    try:
        client   = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=CONFIG["claude_opus_model"],
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        review = response.content[0].text
        logger.info("Weekly review completed")
        return review
    except Exception as e:
        logger.error(f"Weekly review error: {e}")
        return None
