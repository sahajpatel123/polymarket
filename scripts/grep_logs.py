#!/usr/bin/env python3
"""Grep rotated JSON logs by market ID and time range (T1-04).

Usage:
  uv run python scripts/grep_logs.py --log logs/paper.jsonl --condition-id 0xabc
  uv run python scripts/grep_logs.py --log logs/paper.jsonl \\
      --condition-id 0xabc --since 2026-07-21T00:00:00Z --until 2026-07-22T00:00:00Z
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.loggrep import grep_logs, parse_ts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default="logs/paper.jsonl")
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--since", default=None, help="ISO8601 or unix seconds")
    ap.add_argument("--until", default=None, help="ISO8601 or unix seconds")
    ap.add_argument("--event", default=None)
    ap.add_argument("--count-only", action="store_true")
    args = ap.parse_args()

    path = Path(args.log)
    if not path.exists() and not list(path.parent.glob(path.name + ".*") if path.parent.exists() else []):
        print(f"status=NO_LOG path={path}", file=sys.stderr)
        return 0

    rows = grep_logs(
        path,
        condition_id=args.condition_id,
        since=parse_ts(args.since),
        until=parse_ts(args.until),
        event=args.event,
    )
    print(
        f"status=OK matches={len(rows)} condition_id={args.condition_id!r} "
        f"since={args.since!r} until={args.until!r}",
        file=sys.stderr,
    )
    if args.count_only:
        print(json.dumps({"matches": len(rows)}))
        return 0
    for r in rows:
        print(json.dumps(r, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
