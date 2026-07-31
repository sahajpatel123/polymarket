#!/bin/bash
# Monitor the 4-hour paper session - runs every 30 minutes
# Usage: nohup bash monitor_4hr.sh &

ROOT="/Users/sahajpatel/Code/polymarket"
cd "$ROOT"

LOG="backtest_55/monitor.log"
mkdir -p backtest_55/12h_report

echo "=== Monitor started at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" >> "$LOG"

while true; do
  echo "" >> "$LOG"
  echo "--- Check at $(date -u +%Y-%m-%dT%H:%M:%SZ) ---" >> "$LOG"
  
  # Check if paper process is alive
  if pgrep -f "polymaker run --paper --config-dir backtest_55" > /dev/null 2>&1; then
    echo "PAPER PROCESS: ALIVE" >> "$LOG"
  else
    echo "PAPER PROCESS: DEAD - stopping monitor" >> "$LOG"
    break
  fi
  
  # Check elapsed time
  if [ -f "backtest_55/12h_session.json" ]; then
    python3 - <<'PY' >> "$LOG" 2>&1
import json, time
from datetime import datetime, timezone
try:
    s = json.load(open("backtest_55/12h_session.json"))
    now = time.time()
    elapsed = (now - float(s["start_ts"])) / 3600.0
    remaining = float(s["duration_h"]) - elapsed
    print(f"Elapsed: {elapsed:.2f}h / {s['duration_h']}h | Remaining: {remaining:.2f}h")
except Exception as e:
    print(f"Session check error: {e}")
PY
  fi
  
  # Data accumulation
  echo "paper.jsonl lines: $(wc -l < livecfg/journal/paper.jsonl 2>/dev/null || echo 0)" >> "$LOG"
  echo "metrics lines: $(wc -l < livecfg/logs/metrics-paper.jsonl 2>/dev/null || echo 0)" >> "$LOG"
  echo "requote count: $(grep -c 'requote' backtest_55/logs/stdout.log 2>/dev/null || echo 0)" >> "$LOG"
  
  # Health check
  uv run python scripts/paper_health.py --max-age-s 600 >> "$LOG" 2>&1
  
  # Check if session JSON says completed
  if [ -f "backtest_55/12h_session.json" ]; then
    status=$(python3 -c "import json; print(json.load(open('backtest_55/12h_session.json')).get('status','unknown'))" 2>/dev/null)
    if [ "$status" = "completed" ]; then
      echo "SESSION COMPLETED - generating report..." >> "$LOG"
      break
    fi
  fi
  
  # Sleep 30 minutes
  sleep 1800
done

# Session ended - run analysis
echo "=== Session ended, running analysis ===" >> "$LOG"
uv run python scripts/paper_12h_report.py --session backtest_55/12h_session.json --repo "$ROOT" >> "$LOG" 2>&1 || echo "Report generation failed" >> "$LOG"
