#!/usr/bin/env bash
# 12-hour paper validation session + automatic return report.
#
# Usage (from repo root):
#   nohup bash scripts/paper_12h_session.sh >> livecfg/logs/12h_session.log 2>&1 &
#
# Duration default 12h. Override: DURATION_H=12 bash scripts/paper_12h_session.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DURATION_H="${DURATION_H:-12}"
CONFIG_DIR="${CONFIG_DIR:-livecfg}"
BANKROLL="${BANKROLL:-100}"
PROFILE="${PROFILE:-live_scaled}"
LOG_DIR="${CONFIG_DIR}/logs"
mkdir -p "$LOG_DIR" "$CONFIG_DIR/journal" "$CONFIG_DIR/12h_report"

START_TS="$(python3 -c 'import time; print(time.time())')"
START_ISO="$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
END_TS="$(python3 -c "print(float('${START_TS}') + float('${DURATION_H}') * 3600.0)")"

SESSION_JSON="${CONFIG_DIR}/12h_session.json"
python3 - <<PY
import json
from pathlib import Path
Path("${SESSION_JSON}").write_text(json.dumps({
    "status": "running",
    "start_ts": float("${START_TS}"),
    "start_iso": "${START_ISO}",
    "planned_end_ts": float("${END_TS}"),
    "duration_h": float("${DURATION_H}"),
    "config_dir": "${CONFIG_DIR}",
    "profile": "${PROFILE}",
    "bankroll_usdc": float("${BANKROLL}"),
    "report_dir": "${CONFIG_DIR}/12h_report",
    "pid_file": "${CONFIG_DIR}/12h_paper.pid",
}, indent=2) + "\n")
print("session written", "${SESSION_JSON}")
PY

# Rotate live paper logs so the session window is clean (keep archives)
for f in paper.jsonl metrics-paper.jsonl; do
  if [[ -f "${LOG_DIR}/${f}" ]]; then
    mv "${LOG_DIR}/${f}" "${LOG_DIR}/${f}.pre12h.${START_TS}"
    echo "archived ${LOG_DIR}/${f}"
  fi
done
if [[ -f "${CONFIG_DIR}/journal/paper.jsonl" ]]; then
  mv "${CONFIG_DIR}/journal/paper.jsonl" "${CONFIG_DIR}/journal/paper.jsonl.pre12h.${START_TS}"
  echo "archived journal"
fi

echo "=== Starting paper collector at ${START_ISO} for ${DURATION_H}h ==="
# One process only
if pgrep -f "polymaker run --paper" >/dev/null 2>&1; then
  echo "WARNING: existing paper process found; stopping..."
  pkill -f "polymaker run --paper" || true
  sleep 2
fi

# Launch paper (reconnects on upstream outage)
uv run polymaker run --paper --config-dir "${CONFIG_DIR}" \
  >> "${LOG_DIR}/collector-12h-stdout.log" 2>&1 &
PAPER_PID=$!
echo "${PAPER_PID}" > "${CONFIG_DIR}/12h_paper.pid"
echo "paper_pid=${PAPER_PID}"

# Sleep until planned end (or paper dies early — still report)
REMAIN="$(python3 -c "import time; print(max(0, float('${END_TS}') - time.time()))")"
echo "sleeping ${REMAIN}s until report..."
# Heartbeat every 30 min into session log
python3 - <<PY
import os, signal, subprocess, time, json
from pathlib import Path

end = float("${END_TS}")
pid = int("${PAPER_PID}")
log = Path("${LOG_DIR}/12h_heartbeat.log")

def alive(p: int) -> bool:
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False

while time.time() < end:
    now = time.time()
    left = end - now
    row = {
        "ts": now,
        "paper_alive": alive(pid),
        "hours_left": round(left / 3600, 3),
    }
    # light health probe
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "scripts/paper_health.py", "--max-age-s", "600"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        row["health_rc"] = proc.returncode
        row["health_tail"] = (proc.stderr or proc.stdout or "")[-300:]
    except Exception as exc:  # noqa: BLE001
        row["health_err"] = str(exc)
    with log.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    # wake at most every 30 min, or remaining time
    time.sleep(min(1800.0, max(5.0, left)))

print("window complete; stopping paper if still running")
if alive(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(3)
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
print("generating report...")
PY

uv run python scripts/paper_12h_report.py --session "${SESSION_JSON}" --repo "${ROOT}" \
  | tee "${LOG_DIR}/12h_report_generation.log"

echo "=== 12h session finished ==="
cat "${CONFIG_DIR}/12h_report/REPORT_LATEST.md" 2>/dev/null | head -80 || true
