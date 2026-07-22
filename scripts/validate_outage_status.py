#!/usr/bin/env python3
"""Validate compact logs/outage_status.json for monitors (Tier-1 ops).

Checks required keys and optional freshness of ``ts`` while an outage is open.
Does not change strategy math.

Usage:
  uv run python scripts/validate_outage_status.py
  uv run python scripts/validate_outage_status.py --max-age-s 900
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_KEYS = (
    "ts",
    "outage_open",
    "outage_total_h",
    "outage_alert",
    "outage_alert_severe",
    "hours_to_tier2_gate",
    "runtime_h",
    "quotes",
)

RECOMMENDED_KEYS = (
    "connectivity",
    "tier2_allowed",
    "gate_reason",
    "runtime_basis",
)


def _parse_ts(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def validate_status(
    data: dict[str, Any],
    *,
    max_age_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    missing = [k for k in REQUIRED_KEYS if k not in data]
    recommended_missing = [k for k in RECOMMENDED_KEYS if k not in data]
    age_s: float | None = None
    stale = False
    ts = _parse_ts(data.get("ts"))
    if ts is not None:
        age_s = round((now if now is not None else datetime.now(timezone.utc).timestamp()) - ts, 1)
        if max_age_s is not None and bool(data.get("outage_open")) and age_s > max_age_s:
            stale = True
    ok = not missing and not stale
    return {
        "ok": ok,
        "missing": missing,
        "recommended_missing": recommended_missing,
        "age_s": age_s,
        "stale": stale,
        "outage_open": data.get("outage_open"),
        "outage_total_h": data.get("outage_total_h"),
        "hours_to_tier2_gate": data.get("hours_to_tier2_gate"),
        "tier2_allowed": data.get("tier2_allowed"),
        "connectivity": data.get("connectivity"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default="logs/outage_status.json")
    ap.add_argument(
        "--max-age-s",
        type=float,
        default=None,
        help="If set, fail when outage_open and ts older than this many seconds",
    )
    args = ap.parse_args()
    path = Path(args.path)
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

    rep = validate_status(data, max_age_s=args.max_age_s)
    rep["path"] = str(path)
    print(json.dumps(rep, indent=2, sort_keys=True))
    miss = ",".join(rep["missing"]) if rep["missing"] else "-"
    rec = ",".join(rep["recommended_missing"]) if rep["recommended_missing"] else "-"
    print(
        f"status={'OK' if rep['ok'] else 'FAIL'} "
        f"missing={miss} recommended_missing={rec} "
        f"age_s={rep['age_s']} stale={rep['stale']} "
        f"outage_open={rep['outage_open']} "
        f"hours_to_tier2_gate={rep['hours_to_tier2_gate']} "
        f"tier2_allowed={rep['tier2_allowed']}",
        file=sys.stderr,
    )
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
