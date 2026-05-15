#!/bin/bash
# ============================================================
# keep_alive.sh — Auto-restart on crash (run this on VPS)
# Usage: bash keep_alive.sh
# Or with screen: screen -S trader bash keep_alive.sh
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/keep_alive.log"

mkdir -p "$SCRIPT_DIR/logs"

echo "$(date '+%Y-%m-%d %H:%M:%S') Keep-alive started" >> "$LOG_FILE"

while true; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') Starting main.py..." | tee -a "$LOG_FILE"
    cd "$SCRIPT_DIR"
    python3 main.py >> "$LOG_FILE" 2>&1
    EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S') main.py exited (code $EXIT_CODE). Restarting in 30s..." | tee -a "$LOG_FILE"
    sleep 30
done
