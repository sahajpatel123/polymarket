#!/usr/bin/env python3
"""One-shot strategy-loop tick: connectivity + C-01 + summarize (+ optional append).

Tier-1 ops helper for Agent-1 10m cycles. Does not change strategy math.

Default is diagnose-only (no collector restart, no cycle append). Pass
``--append`` to record a trail row; recovery relaunch stays on
``await_polymarket_recovery.py``.

Usage:
  uv run python scripts/strategy_tick.py
  uv run python scripts/strategy_tick.py --append --skip-connectivity
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _status_line(stderr: str, stdout: str = "") -> str:
    for line in stderr.splitlines() + stdout.splitlines():
        if line.startswith("status="):
            return line
    return "status=UNKNOWN"


def _parse_kv(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in line.split():
        if "=" in part:
            k, _, v = part.partition("=")
            out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--append",
        action="store_true",
        help="Also run append_strategy_cycle (records trail row)",
    )
    ap.add_argument(
        "--skip-connectivity",
        action="store_true",
        help="Skip Polymarket probe (faster during known outages)",
    )
    ap.add_argument(
        "--with-counterfactual",
        action="store_true",
        help="When --append, also record offline C-01 counterfactual",
    )
    args = ap.parse_args()
    py = sys.executable
    report: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "steps": {},
    }

    if not args.skip_connectivity:
        code, out, err = _run([
            py,
            "scripts/await_polymarket_recovery.py",
            "--once",
            "--no-restart-on-recover",
            "--no-append-cycle-on-recover",
        ])
        line = _status_line(err, out)
        report["steps"]["connectivity"] = {
            "rc": code,
            "status_line": line,
            **_parse_kv(line),
        }
    else:
        report["steps"]["connectivity"] = {"status": "SKIPPED"}

    code, out, err = _run([py, "scripts/c01_promotion_checklist.py"])
    line = _status_line(err, out)
    report["steps"]["c01"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    code, out, err = _run([py, "scripts/summarize_strategy_cycles.py"])
    line = _status_line(err, out)
    report["steps"]["summarize"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    if args.append:
        cmd = [py, "scripts/append_strategy_cycle.py"]
        if args.skip_connectivity:
            cmd.append("--skip-connectivity")
        if args.with_counterfactual:
            cmd.append("--with-counterfactual")
        code, out, err = _run(cmd)
        line = _status_line(err, out)
        report["steps"]["append"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    print(json.dumps(report, indent=2, sort_keys=True))
    conn = report["steps"].get("connectivity") or {}
    c01 = report["steps"].get("c01") or {}
    sm = report["steps"].get("summarize") or {}
    ap_step = report["steps"].get("append") or {}
    print(
        f"status=OK "
        f"connectivity={conn.get('status') or conn.get('status_line', 'SKIPPED')} "
        f"c01={c01.get('status')} blockers={c01.get('blockers')} "
        f"outage_alert={c01.get('outage_alert')} "
        f"runtime_h={sm.get('runtime_h')} eta_paused={sm.get('eta_paused')} "
        f"tape_frozen={sm.get('tape_frozen')} "
        f"append={ap_step.get('status') or ('SKIPPED' if not args.append else 'UNKNOWN')}",
        file=sys.stderr,
    )
    # Non-zero only if checklist crashed (rc not in 0/1) or summarize failed hard.
    bad = False
    if report["steps"]["c01"]["rc"] not in (0, 1):
        bad = True
    if report["steps"]["summarize"]["rc"] != 0:
        bad = True
    if args.append and report["steps"]["append"]["rc"] != 0:
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
