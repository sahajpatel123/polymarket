#!/bin/bash
set -euo pipefail
ROOT="/Users/sahajpatel/Code/polymarket"
cd "$ROOT"

DURATION_H=4
CONFIG_DIR="backtest_55"
BANKROLL=55
PROFILE="live_scaled"
LOG_DIR="${CONFIG_DIR}/logs"
mkdir -p "${CONFIG_DIR}/journal" "${CONFIG_DIR}/logs" "${CONFIG_DIR}/12h_report"

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
    "pid_file": "${CONFIG_DIR}/pid",
}, indent=2) + "\n")
print("session written", "${SESSION_JSON}")
PY

if pgrep -f "polymaker run --paper" >/dev/null 2>&1; then
  echo "WARNING: existing paper process found; stopping..."
  pkill -f "polymaker run --paper" || true
  sleep 2
fi

# Clean previous paper/metrics logs for this session run
rm -f "${LOG_DIR}/paper.jsonl" "${LOG_DIR}/metrics-paper.jsonl"

echo "=== Starting 4h paper session at ${START_ISO} ==="
uv run polymaker run --paper --config-dir "${CONFIG_DIR}" \
  >> "${LOG_DIR}/stdout.log" 2>&1 &
PAPER_PID=$!
echo "${PAPER_PID}" > "${CONFIG_DIR}/pid"
echo "paper_pid=${PAPER_PID}"

REMAIN="$(python3 -c "import time; print(max(0, float('${END_TS}') - time.time()))")"
echo "sleeping ${REMAIN}s until report..."

python3 - <<PY
import os, signal, subprocess, time, json
from pathlib import Path

end = float("${END_TS}")
pid = int("${PAPER_PID}")
log = Path("${CONFIG_DIR}/12h_heartbeat.log")

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
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "scripts/paper_health.py", "--max-age-s", "600"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        row["health_rc"] = proc.returncode
        row["health_tail"] = (proc.stderr or proc.stdout or "")[-300:]
    except Exception as exc:
        row["health_err"] = str(exc)
    with log.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    time.sleep(min(1800.0, max(5.0, left)))

print("4h window complete; stopping paper if still running")
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

uv run python scripts/paper_12h_report.py --session "${CONFIG_DIR}/12h_session.json" --repo "${ROOT}" \
  | tee "${LOG_DIR}/12h_report_generation.log"

echo "=== 4h paper session finished ==="
