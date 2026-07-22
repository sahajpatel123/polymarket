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
    ap.add_argument(
        "--write-weekly",
        action="store_true",
        help="Also overwrite WEEKLY_REPORT.md from live script outputs",
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

    code, out, err = _run([py, "scripts/unused_knob_toml_scan.py"])
    line = _status_line(err, out)
    report["steps"]["unused_knobs"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    code, out, err = _run([
        py,
        "scripts/outage_window_report.py",
        "--status-out",
        "logs/outage_status.json",
    ])
    line = _status_line(err, out)
    report["steps"]["outage"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    if args.append:
        cmd = [py, "scripts/append_strategy_cycle.py"]
        if args.skip_connectivity:
            cmd.append("--skip-connectivity")
        if args.with_counterfactual:
            cmd.append("--with-counterfactual")
        code, out, err = _run(cmd)
        line = _status_line(err, out)
        report["steps"]["append"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    if args.write_weekly:
        code, out, err = _run([py, "scripts/write_weekly_report.py"])
        line = _status_line(err, out)
        report["steps"]["weekly"] = {"rc": code, "status_line": line, **_parse_kv(line)}

    print(json.dumps(report, indent=2, sort_keys=True))
    conn = report["steps"].get("connectivity") or {}
    c01 = report["steps"].get("c01") or {}
    sm = report["steps"].get("summarize") or {}
    unused = report["steps"].get("unused_knobs") or {}
    outage = report["steps"].get("outage") or {}
    ap_step = report["steps"].get("append") or {}
    weekly = report["steps"].get("weekly") or {}
    conn_line = str(conn.get("status_line") or "")
    if conn.get("status") == "SKIPPED":
        conn_disp = "SKIPPED"
    elif "STILL_DOWN" in conn_line or "TIMEOUT" in conn_line or "RECOVERED" in conn_line:
        conn_disp = conn_line.split()[0].split("=", 1)[-1]
    else:
        conn_disp = conn.get("status") or conn_line or "UNKNOWN"
    print(
        f"status=OK "
        f"connectivity={conn_disp} "
        f"c01={c01.get('status')} blockers={c01.get('blockers')} "
        f"outage_alert={c01.get('outage_alert') or outage.get('outage_alert')} "
        f"outage_alert_severe={c01.get('outage_alert_severe') or outage.get('outage_alert_severe')} "
        f"outage_total_h={outage.get('total_h')} "
        f"runtime_h={sm.get('runtime_h')} eta_paused={sm.get('eta_paused')} "
        f"tape_frozen={sm.get('tape_frozen')} "
        f"unused_set={unused.get('n_set_unused')} "
        f"append={ap_step.get('status') or ('SKIPPED' if not args.append else 'UNKNOWN')} "
        f"weekly={weekly.get('status') or ('SKIPPED' if not args.write_weekly else 'UNKNOWN')}",
        file=sys.stderr,
    )
    # Non-zero only if checklist crashed (rc not in 0/1) or summarize failed hard.
    bad = False
    if report["steps"]["c01"]["rc"] not in (0, 1):
        bad = True
    if report["steps"]["summarize"]["rc"] != 0:
        bad = True
    if report["steps"]["unused_knobs"]["rc"] != 0:
        bad = True
    if report["steps"]["outage"]["rc"] not in (0, 2):
        bad = True
    if args.append and report["steps"]["append"]["rc"] != 0:
        bad = True
    if args.write_weekly and report["steps"]["weekly"]["rc"] != 0:
        bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
