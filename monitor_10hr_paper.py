#!/usr/bin/env python3
"""Monitor 10h paper session and generate report at the end."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/Users/sahajpatel/Code/polymarket")
CONFIG_DIR = "livecfg"
DURATION_H = 10
BANKROLL = 50
PROFILE = "live_scaled"

START_TS = time.time()
START_ISO = datetime.now(timezone.utc).isoformat()
END_TS = START_TS + DURATION_H * 3600.0

SESSION_JSON = ROOT / CONFIG_DIR / "10h_session.json"
SESSION_JSON.write_text(json.dumps({
    "status": "running",
    "start_ts": START_TS,
    "start_iso": START_ISO,
    "planned_end_ts": END_TS,
    "duration_h": DURATION_H,
    "config_dir": CONFIG_DIR,
    "profile": PROFILE,
    "bankroll_usdc": BANKROLL,
    "report_dir": f"{CONFIG_DIR}/10h_report",
    "pid_file": f"{CONFIG_DIR}/10h_paper.pid",
}, indent=2) + "\n")
print(f"Session written to {SESSION_JSON}")

# Find the paper process
result = subprocess.run(["pgrep", "-f", "polymaker run --paper"], capture_output=True, text=True)
pids = result.stdout.strip().split("\n") if result.stdout.strip() else []
if pids:
    pid = int(pids[0])
    print(f"Found paper process: {pid}")
else:
    print("ERROR: No paper process found!")
    sys.exit(1)

# Save PID
(ROOT / CONFIG_DIR / "10h_paper.pid").write_text(str(pid))

log_file = ROOT / CONFIG_DIR / "10h_heartbeat.log"

def alive(p: int) -> bool:
    try:
        os.kill(p, 0)
        return True
    except OSError:
        return False

print(f"Monitoring for {DURATION_H} hours (until {datetime.fromtimestamp(END_TS, timezone.utc).isoformat()})")

while time.time() < END_TS:
    now = time.time()
    left = END_TS - now
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

    with log_file.open("a") as fh:
        fh.write(json.dumps(row) + "\n")

    # Print status every 10 minutes
    if int(left) % 600 == 0:
        print(f"  {row['hours_left']:.2f}h left, paper_alive={row['paper_alive']}")

    sleep_time = min(1800.0, max(5.0, left))
    time.sleep(sleep_time)

print(f"\n{DURATION_H}h window complete; stopping paper if still running")
if alive(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(3)
        if alive(pid):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

print("Generating report...")

# Generate report using the same pattern as 12h report
report_proc = subprocess.run(
    [
        sys.executable,
        "scripts/paper_12h_report.py",
        "--session",
        str(SESSION_JSON),
        "--repo",
        str(ROOT),
    ],
    cwd=str(ROOT),
    capture_output=True,
    text=True,
)

print(report_proc.stdout)
if report_proc.stderr:
    print(report_proc.stderr, file=sys.stderr)

log_path = ROOT / CONFIG_DIR / "logs" / "10h_report_generation.log"
log_path.write_text(report_proc.stdout + "\n" + report_proc.stderr)

print("=== 10h paper session finished ===")