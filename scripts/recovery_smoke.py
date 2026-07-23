#!/usr/bin/env python3
"""Post-recovery smoke checklist for paper collection (Tier-1 ops).

After Polymarket REST/WS returns, confirm the collector is healthy and the
Tier-2 gate is reading a coherent paper-log family. Does not change strategy
math.

Usage:
  uv run python scripts/recovery_smoke.py
  uv run python scripts/recovery_smoke.py --status logs/outage_status.json
  uv run python scripts/recovery_smoke.py --min-quotes 5529
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def evaluate_recovery(
    status: dict[str, Any],
    *,
    min_quotes: int | None = None,
    require_health_ok: bool = True,
) -> dict[str, Any]:
    """Return checklist result for a compact outage_status snapshot."""
    checks: dict[str, bool] = {}
    blockers: list[str] = []

    conn = str(status.get("connectivity") or "")
    # polymarket_connectivity prints status=OK when REST+WS are up (not status=UP).
    up = (
        status.get("recovered") is True
        or "status=UP" in conn
        or (
            "status=OK" in conn
            and "rest_ok=True" in conn
            and "ws_ok=True" in conn
        )
        or (conn.startswith("status=OK") and "rest_ok=False" not in conn)
    )
    checks["connectivity_up"] = up
    if not up:
        blockers.append("connectivity_up")

    open_outage = bool(status.get("outage_open"))
    checks["outage_closed"] = not open_outage
    if open_outage:
        blockers.append("outage_closed")

    health = str(status.get("health") or "")
    if require_health_ok:
        checks["health_ok"] = health == "OK"
        if health != "OK":
            blockers.append("health_ok")
    else:
        checks["health_ok"] = health in {"OK", "STALE", ""}

    tape = status.get("tape_frozen")
    checks["tape_unfrozen"] = tape is False or str(tape).lower() == "false"
    if not checks["tape_unfrozen"]:
        blockers.append("tape_unfrozen")

    basis = str(status.get("runtime_basis") or "")
    checks["runtime_basis_requote"] = basis == "requote" or basis == ""
    if basis and basis != "requote":
        blockers.append("runtime_basis_requote")

    files = status.get("paper_log_files")
    try:
        n_files = int(files) if files is not None else 1
    except (TypeError, ValueError):
        n_files = 0
    checks["paper_log_family"] = n_files >= 1 and bool(status.get("paper_log"))
    if not checks["paper_log_family"]:
        blockers.append("paper_log_family")

    if min_quotes is not None:
        try:
            quotes = int(float(status.get("quotes") or 0))
        except (TypeError, ValueError):
            quotes = 0
        checks["quotes_floor"] = quotes >= min_quotes
        if quotes < min_quotes:
            blockers.append("quotes_floor")
    else:
        checks["quotes_floor"] = True

    # Critical/imminent alerts should clear after recovery.
    for key in (
        "outage_alert",
        "outage_alert_severe",
        "outage_alert_prolonged",
        "outage_alert_critical",
        "outage_alert_imminent",
        "outage_alert_final",
        "outage_alert_critical_aged",
        "outage_alert_critical_hour",
    ):
        val = status.get(key)
        cleared = val is False or val is None or str(val).lower() == "false"
        checks[f"{key}_cleared"] = cleared
        if not cleared:
            blockers.append(f"{key}_cleared")

    ok = not blockers
    return {
        "ok": ok,
        "checks": checks,
        "blockers": blockers,
        "connectivity": status.get("connectivity"),
        "health": status.get("health"),
        "outage_open": status.get("outage_open"),
        "quotes": status.get("quotes"),
        "runtime_basis": status.get("runtime_basis"),
        "paper_log": status.get("paper_log"),
        "paper_log_files": status.get("paper_log_files"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", default="logs/outage_status.json")
    ap.add_argument(
        "--min-quotes",
        type=int,
        default=None,
        help="Fail if quotes dropped below this floor (post-recovery)",
    )
    ap.add_argument(
        "--allow-stale-health",
        action="store_true",
        help="Do not require health=OK (diagnose-only during outage)",
    )
    args = ap.parse_args()
    path = Path(args.status)
    if not path.exists():
        print(json.dumps({"ok": False, "error": "missing_file", "path": str(path)}, indent=2))
        print(f"status=MISSING path={path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": "invalid_json", "detail": str(exc)}, indent=2))
        print(f"status=INVALID path={path}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(json.dumps({"ok": False, "error": "not_object"}, indent=2))
        print(f"status=INVALID path={path}", file=sys.stderr)
        return 2

    rep = evaluate_recovery(
        data,
        min_quotes=args.min_quotes,
        require_health_ok=not args.allow_stale_health,
    )
    rep["path"] = str(path)
    print(json.dumps(rep, indent=2, sort_keys=True))
    blockers = ",".join(rep["blockers"]) if rep["blockers"] else "-"
    print(
        f"status={'PASS' if rep['ok'] else 'FAIL'} "
        f"blockers={blockers} "
        f"health={rep['health']} outage_open={rep['outage_open']} "
        f"quotes={rep['quotes']} runtime_basis={rep['runtime_basis']} "
        f"paper_log_files={rep['paper_log_files']}",
        file=sys.stderr,
    )
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
