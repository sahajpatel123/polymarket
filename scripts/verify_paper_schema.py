#!/usr/bin/env python3
"""Verify recent paper requote events carry expected schema fields.

Catches stale paper collectors that predate logging upgrades (e.g. T1-41
``vol_ratio``). Tier-1 ops check — does not change strategy math.

Usage:
  uv run python scripts/verify_paper_schema.py
  uv run python scripts/verify_paper_schema.py --tail 50 --require vol_ratio,flowz,regime
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.metrics.log_discovery import DEFAULT_PAPER_CANDIDATES, pick_richest_log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=None)
    ap.add_argument(
        "--tail",
        type=int,
        default=50,
        help="Inspect this many most-recent requote events",
    )
    ap.add_argument(
        "--require",
        default="vol_ratio,flowz,regime,fv,cancel,place",
        help="Comma-separated required requote fields",
    )
    args = ap.parse_args()
    path = (
        Path(args.log)
        if args.log
        else pick_richest_log(DEFAULT_PAPER_CANDIDATES)
        or Path(DEFAULT_PAPER_CANDIDATES[0])
    )
    if not path.exists():
        print(f"status=NO_LOG path={path}", file=sys.stderr)
        return 2

    required = [f.strip() for f in args.require.split(",") if f.strip()]
    requotes: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and (
                obj.get("event") == "requote" or obj.get("msg") == "requote"
            ):
                requotes.append(obj)
    recent = requotes[-args.tail :] if args.tail > 0 else requotes
    if not recent:
        print("status=NO_REQUOTES", file=sys.stderr)
        return 2

    n = len(recent)
    missing_counts = {f: 0 for f in required}
    for q in recent:
        for f in required:
            if f not in q or q.get(f) is None:
                missing_counts[f] += 1

    latest = recent[-1]
    latest_missing = [f for f in required if f not in latest or latest.get(f) is None]
    payload = {
        "path": str(path),
        "n_requotes_total": len(requotes),
        "n_requotes_checked": n,
        "required": required,
        "missing_counts": missing_counts,
        "missing_frac": {f: round(missing_counts[f] / n, 6) for f in required},
        "latest_ts": latest.get("timestamp") or latest.get("ts"),
        "latest_missing": latest_missing,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if latest_missing:
        print(
            f"status=STALE_SCHEMA missing={','.join(latest_missing)} "
            f"checked={n} latest_ts={payload['latest_ts']}",
            file=sys.stderr,
        )
        return 1
    bad_any = [f for f, c in missing_counts.items() if c > 0]
    if bad_any:
        print(
            f"status=CATCHING_UP latest_ok=true legacy_missing={','.join(bad_any)} "
            f"checked={n} latest_ts={payload['latest_ts']}",
            file=sys.stderr,
        )
        return 0
    print(f"status=OK checked={n} required={','.join(required)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
