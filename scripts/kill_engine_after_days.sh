#!/usr/bin/env bash
# Kill the paper collector engine after N days (default 14).
# Matches by command pattern so it works across engine restarts and never
# touches unrelated processes. Self-match is avoided via the [p] bracket trick.
set -u

DAYS="${1:-14}"
LOG="logs/kill_engine_reminder.log"

kill_target() {
  pkill -f "[p]olymaker run --paper" 2>/dev/null
  pkill -f "[.]venv/bin/polymaker run --paper" 2>/dev/null
}

START=$(date '+%Y-%m-%d %H:%M:%S %Z')
TARGET=$(date -v +"${DAYS}"d '+%Y-%m-%d %H:%M:%S %Z')
echo "[$(date '+%Y-%m-%d %H:%M:%S')] reminder armed: ${DAYS} days from ${START} -> kill at ${TARGET}" >> "$LOG"

sleep "$((DAYS * 86400))"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] deadline reached; killing paper collector" >> "$LOG"
kill_target
echo "[$(date '+%Y-%m-%d %H:%M:%S')] kill issued; exit=$?" >> "$LOG"
