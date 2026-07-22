#!/usr/bin/env python3
"""Poll Polymarket connectivity and relaunch paper collector when it recovers.

Tier-1 ops for long outages (REST/WS DOWN). Does not change strategy math.

Usage:
  uv run python scripts/await_polymarket_recovery.py --once
  uv run python scripts/await_polymarket_recovery.py --interval-s 60 --max-wait-s 3600
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _status_line(stderr: str, stdout: str = "") -> str:
    for line in (stderr.splitlines() + stdout.splitlines()):
        if line.startswith("status="):
            return line
    return "status=UNKNOWN"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--timeout-s", type=float, default=5.0)
    ap.add_argument("--interval-s", type=float, default=60.0)
    ap.add_argument("--max-wait-s", type=float, default=0.0,
                    help="0 = poll forever until UP or --once")
    ap.add_argument("--once", action="store_true",
                    help="Single connectivity check (no loop)")
    ap.add_argument("--no-restart-on-recover", action="store_true",
                    help="Do not relaunch collector when connectivity returns")
    ap.add_argument("--wait-s", type=float, default=45.0,
                    help="ensure_paper_collector post-restart health wait")
    args = ap.parse_args()
    restart = not args.no_restart_on_recover

    py = sys.executable
    t0 = time.time()
    attempt = 0
    while True:
        attempt += 1
        code, out, err = _run([
            py, "scripts/polymarket_connectivity.py",
            "--timeout-s", str(args.timeout_s),
        ])
        line = _status_line(err, out)
        ts = datetime.now(timezone.utc).isoformat()
        print(json.dumps({
            "ts": ts,
            "attempt": attempt,
            "connectivity_rc": code,
            "connectivity": line,
        }), flush=True)

        if code == 0 and "status=OK" in line:
            report = {
                "ts": ts,
                "recovered": True,
                "attempts": attempt,
                "waited_s": round(time.time() - t0, 1),
                "connectivity": line,
            }
            if restart:
                rcode, rout, rerr = _run([
                    py, "scripts/ensure_paper_collector.py",
                    "--config-dir", args.config_dir,
                    "--restart",
                    "--force-restart",
                    "--wait-s", str(args.wait_s),
                ])
                report["ensure_rc"] = rcode
                report["ensure"] = _status_line(rerr, rout)
            print(json.dumps(report, indent=2, sort_keys=True))
            print(
                f"status=RECOVERED waited_s={report['waited_s']} "
                f"ensure={report.get('ensure', 'skipped')}",
                file=sys.stderr,
            )
            return 0 if (not restart or report.get("ensure_rc") == 0) else 1

        if args.once:
            print(f"status=STILL_DOWN {line}", file=sys.stderr)
            return 1

        if args.max_wait_s > 0 and (time.time() - t0) >= args.max_wait_s:
            print(
                f"status=TIMEOUT waited_s={round(time.time() - t0, 1)} {line}",
                file=sys.stderr,
            )
            return 1

        time.sleep(max(1.0, args.interval_s))


if __name__ == "__main__":
    raise SystemExit(main())
