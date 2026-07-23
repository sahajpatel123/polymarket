#!/usr/bin/env python3
"""One-line operator brief from logs/outage_status.json (Tier-1 ops).

Summarizes outage severity and the next recovery action. Does not change
strategy math.

Usage:
  uv run python scripts/outage_operator_brief.py
  uv run python scripts/outage_operator_brief.py --path logs/outage_status.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def operator_brief(status: dict[str, Any]) -> dict[str, Any]:
    """Derive a compact operator mode + next action from compact status."""
    open_outage = bool(status.get("outage_open"))
    critical = bool(status.get("outage_alert_critical"))
    recovered = bool(status.get("recovered"))
    conn = str(status.get("connectivity") or "")
    up = recovered or (
        "status=OK" in conn and "rest_ok=True" in conn and "ws_ok=True" in conn
    ) or "status=UP" in conn

    if recovered:
        mode = "RECOVERED"
        action = "run_recovery_smoke"
    elif critical and open_outage:
        mode = "CRITICAL_OPEN"
        action = "await_UP_then_full_recovery"
    elif open_outage:
        mode = "OUTAGE_OPEN"
        action = "await_UP_diagnose_only"
    elif up:
        mode = "QUIET"
        action = "continue_paper_gate"
    else:
        mode = "QUIET"
        action = "continue_paper_gate"

    return {
        "mode": mode,
        "action": action,
        "outage_open": open_outage,
        "outage_total_h": status.get("outage_total_h"),
        "outage_alert_critical": critical,
        "minutes_past_critical": status.get("minutes_past_critical"),
        "hours_past_critical": status.get("hours_past_critical"),
        "quotes": status.get("quotes"),
        "runtime_h": status.get("runtime_h"),
        "hours_to_tier2_gate": status.get("hours_to_tier2_gate"),
        "connectivity": conn or None,
        "c01_status": status.get("c01_status"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default="logs/outage_status.json")
    args = ap.parse_args()
    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"ok": False, "error": "missing_file", "path": str(path)}))
        print(f"status=MISSING path={path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": "invalid_json", "detail": str(exc)}))
        print(f"status=INVALID path={path}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(json.dumps({"ok": False, "error": "not_object"}))
        print(f"status=INVALID path={path}", file=sys.stderr)
        return 2

    brief = operator_brief(data)
    brief["ok"] = True
    brief["path"] = str(path)
    print(json.dumps(brief, indent=2, sort_keys=True))
    print(
        f"status={brief['mode']} action={brief['action']} "
        f"outage_total_h={brief['outage_total_h']} "
        f"minutes_past_critical={brief['minutes_past_critical']} "
        f"quotes={brief['quotes']} "
        f"c01={brief['c01_status'] or '-'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
