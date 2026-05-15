# ============================================================
# news_filter.py — Economic calendar news avoidance
# Free ForexFactory JSON feed. Caches 60 min.
# Blocks trading 30 min before / 15 min after high impact news.
# ============================================================

import logging
import requests
from datetime import datetime, timedelta
import pytz
from config import CONFIG, PAIR_CURRENCIES

logger = logging.getLogger(__name__)

_news_cache      = []
_cache_timestamp = None
_cache_ttl_mins  = 60


def _fetch_news():
    """Fetch this week's calendar. Returns list or empty list."""
    global _news_cache, _cache_timestamp

    now = datetime.now(pytz.UTC)

    if (_cache_timestamp and
            (now - _cache_timestamp).total_seconds() < _cache_ttl_mins * 60):
        return _news_cache

    try:
        url      = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        events           = response.json()
        _news_cache      = events
        _cache_timestamp = now
        high_count = sum(1 for e in events if e.get("impact") == "High")
        logger.info(f"News calendar refreshed: {len(events)} events, {high_count} high impact")
        return events
    except requests.RequestException as e:
        logger.warning(f"News calendar fetch failed: {e} — using stale cache")
        return _news_cache
    except Exception as e:
        logger.error(f"News calendar error: {e}")
        return _news_cache


def is_news_blocked(pair):
    """
    Check if this pair is blocked due to imminent or recent news.

    Returns:
        (blocked: bool, reason: str)
    """
    events = _fetch_news()
    if not events:
        logger.warning("No news data — proceeding without news filter")
        return False, "News data unavailable"

    currencies    = PAIR_CURRENCIES.get(pair, [])
    impact_levels = CONFIG["news_impact_levels"]
    block_before  = CONFIG["news_block_minutes_before"]
    block_after   = CONFIG["news_block_minutes_after"]
    now           = datetime.now(pytz.UTC)

    for event in events:
        if event.get("impact") not in impact_levels:
            continue

        event_currency = event.get("country", "").upper()
        if event_currency not in currencies:
            continue

        try:
            event_time_str = event.get("date", "")
            if not event_time_str:
                continue
            event_time = datetime.fromisoformat(event_time_str.replace("Z", "+00:00"))
            if event_time.tzinfo is None:
                event_time = pytz.UTC.localize(event_time)
        except (ValueError, TypeError):
            continue

        mins_until  = (event_time - now).total_seconds() / 60
        mins_since  = (now - event_time).total_seconds() / 60

        if 0 <= mins_until <= block_before:
            reason = f"{event.get('title', 'News')} ({event_currency}) in {mins_until:.0f}min"
            logger.info(f"{pair} blocked by upcoming news: {reason}")
            return True, reason

        if 0 <= mins_since <= block_after:
            reason = f"{event.get('title', 'News')} ({event_currency}) {mins_since:.0f}min ago"
            logger.info(f"{pair} blocked by recent news: {reason}")
            return True, reason

    return False, "No blocking news"
