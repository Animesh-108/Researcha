# ============================================================
# health_monitor.py — System health checks every 5 minutes
# Checks: MT5 connection, Claude API, Telegram, RAM, database
# Alerts on any failure. Triggers clean restart if RAM critical.
# Runs in a background thread via main.py.
# ============================================================

import os
import gc
import time
import logging
import threading
import psutil
import requests
import anthropic
from dotenv import load_dotenv
from config import CONFIG

load_dotenv()
logger = logging.getLogger(__name__)


class HealthMonitor:
    """
    Runs every 5 minutes in a background thread.
    Checks all critical system components.
    Sends Telegram alert if anything is wrong.
    """

    def __init__(self, data_feed, notifier_module, trade_logger):
        self.feed      = data_feed
        self.notifier  = notifier_module
        self.logger_db = trade_logger
        self._running  = False
        self._thread   = None
        self._interval = CONFIG["health_check_minutes"] * 60
        self._failure_counts = {
            "mt5":      0,
            "claude":   0,
            "telegram": 0,
        }

    def start(self):
        """Start health monitor in background thread."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._health_loop,
            name="HealthMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("Health monitor started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Health monitor stopped")

    # --------------------------------------------------------
    # Main loop
    # --------------------------------------------------------

    def _health_loop(self):
        while self._running:
            try:
                self._run_all_checks()
            except Exception as e:
                logger.error(f"Health monitor error: {e}", exc_info=True)
            time.sleep(self._interval)

    def _run_all_checks(self):
        issues  = []
        ok_list = []

        # 1. MT5 connection
        mt5_ok = self._check_mt5()
        if mt5_ok:
            ok_list.append("MT5 ✅")
            self._failure_counts["mt5"] = 0
        else:
            self._failure_counts["mt5"] += 1
            issues.append(f"MT5 connection lost (failure #{self._failure_counts['mt5']})")

        # 2. Claude API — lightweight check (no actual call, just key present)
        claude_ok = self._check_claude_key()
        if claude_ok:
            ok_list.append("Claude ✅")
        else:
            issues.append("Anthropic API key missing or invalid")

        # 3. Telegram bot
        tg_ok = self._check_telegram()
        if tg_ok:
            ok_list.append("Telegram ✅")
            self._failure_counts["telegram"] = 0
        else:
            self._failure_counts["telegram"] += 1
            issues.append(f"Telegram unreachable (failure #{self._failure_counts['telegram']})")

        # 4. RAM usage
        ram = psutil.virtual_memory().percent
        if ram < CONFIG["max_memory_pct"]:
            ok_list.append(f"RAM {ram:.0f}% ✅")
        else:
            issues.append(f"RAM {ram:.0f}% high — forcing garbage collection")
            gc.collect()
            ram_after = psutil.virtual_memory().percent
            if ram_after >= CONFIG["max_memory_pct"] + 5:
                issues.append(f"RAM still {ram_after:.0f}% after GC — consider restart")

        # 5. Database integrity
        db_ok = self._check_database()
        if db_ok:
            ok_list.append("DB ✅")
        else:
            issues.append("Database integrity check failed")

        # Report
        if issues:
            issue_text = "\n".join(f"⚠️ {i}" for i in issues)
            logger.warning(f"Health issues detected:\n{issue_text}")
            self.notifier.send_message(f"🔴 *HEALTH ALERT*\n\n{issue_text}")
        else:
            logger.debug(f"Health OK: {' | '.join(ok_list)}")

    # --------------------------------------------------------
    # Individual checks
    # --------------------------------------------------------

    def _check_mt5(self):
        """Verify MT5 terminal responds to account_info query."""
        try:
            return self.feed.is_connected()
        except Exception:
            return False

    def _check_claude_key(self):
        """Just check the API key is present and correctly formatted."""
        key = os.getenv("ANTHROPIC_API_KEY", "")
        return key.startswith("sk-ant-") and len(key) > 20

    def _check_telegram(self):
        """Call Telegram getMe endpoint to verify bot token is valid."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False
        try:
            url      = f"https://api.telegram.org/bot{token}/getMe"
            response = requests.get(url, timeout=10)
            return response.status_code == 200 and response.json().get("ok")
        except Exception:
            return False

    def _check_database(self):
        """Run SQLite integrity check."""
        try:
            from sqlalchemy import text
            with self.logger_db.engine.connect() as conn:
                result = conn.execute(text("PRAGMA integrity_check")).fetchone()
                return result and result[0] == "ok"
        except Exception as e:
            logger.error(f"DB integrity check error: {e}")
            return False

    # --------------------------------------------------------
    # Public: full status snapshot for /health command
    # --------------------------------------------------------

    def get_status_dict(self):
        """Return health status dict for Telegram /health command."""
        ram  = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent if os.name != "nt" else psutil.disk_usage("C:\\").percent

        return {
            "mt5":       self._check_mt5(),
            "claude":    self._check_claude_key(),
            "telegram":  self._check_telegram(),
            "database":  self._check_database(),
            "ram_pct":   round(ram, 1),
            "disk_pct":  round(disk, 1),
        }
