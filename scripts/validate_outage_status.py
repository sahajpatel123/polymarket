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
    "outage_alert_prolonged",
    "outage_alert_critical",
    "outage_alert_imminent",
    "outage_alert_final",
    "hours_to_tier2_gate",
    "runtime_h",
    "quotes",
)

# Required only while an outage window is open (T1-103/T1-109/T1-112/T1-114).
OPEN_OUTAGE_REQUIRED_KEYS = (
    "hours_to_critical",
    "minutes_to_critical",
    "outage_started_at",
    "outage_critical_at",
    "hours_to_imminent",
)

# Required while the final-hour imminent alert is lit (T1-111/T1-112).
IMMINENT_REQUIRED_KEYS = (
    "outage_imminent_since",
    "hours_in_imminent",
)

# Required while the ≥12h critical alert is lit (T1-113/T1-117).
CRITICAL_REQUIRED_KEYS = (
    "outage_critical_since",
    "hours_past_critical",
    "minutes_past_critical",
)

RECOMMENDED_KEYS = (
    "connectivity",
    "tier2_allowed",
    "gate_reason",
    "runtime_basis",
    "tape_frozen",
    "eta_paused",
    "last_requote_age_s",
    "last_requote_at",
    "health",
    "ensure_status",
    "collector_pid",
    "deps_ok",
    "n_cycles",
    "c01_status",
    "c01_blockers",
    "paper_log",
    "paper_log_files",
    "metrics_log",
    "recovery_smoke",
    "recovery_smoke_blockers",
)


def _parse_ts(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _present(data: dict[str, Any], key: str) -> bool:
    if key not in data:
        return False
    val = data[key]
    if val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    return True


def validate_status(
    data: dict[str, Any],
    *,
    max_age_s: float | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    missing = [k for k in REQUIRED_KEYS if k not in data]
    open_outage = bool(data.get("outage_open"))
    if open_outage:
        for key in OPEN_OUTAGE_REQUIRED_KEYS:
            if not _present(data, key) and key not in missing:
                missing.append(key)
    if bool(data.get("outage_alert_imminent")):
        for key in IMMINENT_REQUIRED_KEYS:
            if not _present(data, key) and key not in missing:
                missing.append(key)
    if bool(data.get("outage_alert_critical")):
        for key in CRITICAL_REQUIRED_KEYS:
            if not _present(data, key) and key not in missing:
                missing.append(key)
    # Critical-state consistency (T1-116): pre-critical flags must clear,
    # and countdowns must be zero once ≥12h is lit.
    inconsistencies: list[str] = []
    if bool(data.get("outage_alert_critical")):
        if bool(data.get("outage_alert_imminent")):
            inconsistencies.append("imminent_while_critical")
        if bool(data.get("outage_alert_final")):
            inconsistencies.append("final_while_critical")
        mtc = data.get("minutes_to_critical")
        if mtc is not None:
            try:
                if int(mtc) != 0:
                    inconsistencies.append("minutes_to_critical_nonzero")
            except (TypeError, ValueError):
                inconsistencies.append("minutes_to_critical_invalid")
        htc = data.get("hours_to_critical")
        if htc is not None:
            try:
                if float(htc) != 0.0:
                    inconsistencies.append("hours_to_critical_nonzero")
            except (TypeError, ValueError):
                inconsistencies.append("hours_to_critical_invalid")
    recommended_missing = [k for k in RECOMMENDED_KEYS if k not in data]
    age_s: float | None = None
    stale = False
    ts = _parse_ts(data.get("ts"))
    if ts is not None:
        age_s = round((now if now is not None else datetime.now(timezone.utc).timestamp()) - ts, 1)
        if max_age_s is not None and open_outage and age_s > max_age_s:
            stale = True
    ok = not missing and not stale and not inconsistencies
    return {
        "ok": ok,
        "missing": missing,
        "recommended_missing": recommended_missing,
        "inconsistencies": inconsistencies,
        "age_s": age_s,
        "stale": stale,
        "outage_open": data.get("outage_open"),
        "outage_total_h": data.get("outage_total_h"),
        "hours_to_tier2_gate": data.get("hours_to_tier2_gate"),
        "hours_to_critical": data.get("hours_to_critical"),
        "minutes_to_critical": data.get("minutes_to_critical"),
        "outage_started_at": data.get("outage_started_at"),
        "outage_critical_at": data.get("outage_critical_at"),
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
    inconsist = ",".join(rep.get("inconsistencies") or []) or "-"
    print(
        f"status={'OK' if rep['ok'] else 'FAIL'} "
        f"missing={miss} recommended_missing={rec} "
        f"inconsistencies={inconsist} "
        f"age_s={rep['age_s']} stale={rep['stale']} "
        f"outage_open={rep['outage_open']} "
        f"hours_to_tier2_gate={rep['hours_to_tier2_gate']} "
        f"hours_to_critical={rep['hours_to_critical']} "
        f"minutes_to_critical={rep.get('minutes_to_critical')} "
        f"outage_started_at={rep['outage_started_at']} "
        f"outage_critical_at={rep.get('outage_critical_at')} "
        f"tier2_allowed={rep['tier2_allowed']}",
        file=sys.stderr,
    )
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
