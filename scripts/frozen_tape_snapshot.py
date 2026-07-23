#!/usr/bin/env python3
"""Latch a frozen-tape snapshot while paper collection is STALE (Tier-1 ops).

Preserves quotes/runtime at the first freeze so post-recovery comparisons can
tell whether the tape advanced. Does not change strategy math.

Usage:
  uv run python scripts/frozen_tape_snapshot.py
  uv run python scripts/frozen_tape_snapshot.py --status logs/outage_status.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_frozen_snapshot(
    status: dict[str, Any],
    *,
    prev: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Return a snapshot while outage/tape is frozen; None when clear."""
    frozen = bool(status.get("tape_frozen")) or bool(status.get("outage_open"))
    if not frozen:
        return None
    ts = now or status.get("ts") or datetime.now(timezone.utc).isoformat()
    prev = prev or {}
    quotes = status.get("quotes")
    runtime_h = status.get("runtime_h")
    if prev.get("frozen_since") not in (None, "") and prev.get("quotes_at_freeze") is not None:
        quotes_at_freeze = prev.get("quotes_at_freeze")
        runtime_at_freeze = prev.get("runtime_h_at_freeze")
        frozen_since = prev.get("frozen_since")
    else:
        quotes_at_freeze = quotes
        runtime_at_freeze = runtime_h
        frozen_since = ts
    return {
        "ts": ts,
        "frozen": True,
        "frozen_since": frozen_since,
        "quotes": quotes,
        "quotes_at_freeze": quotes_at_freeze,
        "runtime_h": runtime_h,
        "runtime_h_at_freeze": runtime_at_freeze,
        "paper_log": status.get("paper_log"),
        "paper_log_files": status.get("paper_log_files"),
        "last_requote_at": status.get("last_requote_at"),
        "last_requote_age_s": status.get("last_requote_age_s"),
        "outage_total_h": status.get("outage_total_h"),
        "minutes_past_critical": status.get("minutes_past_critical"),
        "operator_mode": status.get("operator_mode"),
        "operator_recovery_cmd": status.get("operator_recovery_cmd"),
        "connectivity": status.get("connectivity"),
        "c01_status": status.get("c01_status"),
    }


def write_frozen_snapshot(
    status_path: Path,
    snapshot_path: Path,
) -> dict[str, Any]:
    """Read status, write/latch snapshot; return result payload."""
    if not status_path.exists():
        return {"ok": False, "error": "missing_status", "path": str(status_path)}
    try:
        status = json.loads(status_path.read_text())
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "invalid_status", "detail": str(exc)}
    if not isinstance(status, dict):
        return {"ok": False, "error": "status_not_object"}

    prev: dict[str, Any] = {}
    if snapshot_path.exists():
        try:
            prev = json.loads(snapshot_path.read_text())
        except json.JSONDecodeError:
            prev = {}

    snap = build_frozen_snapshot(status, prev=prev)
    if snap is None:
        if snapshot_path.exists():
            # Keep last freeze for comparison; mark cleared.
            cleared = dict(prev)
            cleared.update({
                "ts": datetime.now(timezone.utc).isoformat(),
                "frozen": False,
                "cleared": True,
            })
            snapshot_path.write_text(json.dumps(cleared, indent=2, sort_keys=True) + "\n")
            return {"ok": True, "frozen": False, "path": str(snapshot_path), **cleared}
        return {"ok": True, "frozen": False, "path": str(snapshot_path), "skipped": True}

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "path": str(snapshot_path), **snap}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--status", default="logs/outage_status.json")
    ap.add_argument("--out", default="logs/frozen_tape_snapshot.json")
    args = ap.parse_args()
    rep = write_frozen_snapshot(Path(args.status), Path(args.out))
    print(json.dumps(rep, indent=2, sort_keys=True))
    if not rep.get("ok"):
        print(f"status=FAIL error={rep.get('error')}", file=sys.stderr)
        return 2
    if rep.get("frozen"):
        print(
            f"status=FROZEN quotes_at_freeze={rep.get('quotes_at_freeze')} "
            f"quotes={rep.get('quotes')} frozen_since={rep.get('frozen_since')} "
            f"out={args.out}",
            file=sys.stderr,
        )
    elif rep.get("cleared"):
        print(
            f"status=CLEARED quotes_at_freeze={rep.get('quotes_at_freeze')} "
            f"out={args.out}",
            file=sys.stderr,
        )
    else:
        print(f"status=SKIPPED out={args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
