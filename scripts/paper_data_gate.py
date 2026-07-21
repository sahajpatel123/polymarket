#!/usr/bin/env python3
"""Query paper-trading log coverage for Autonomous Loop Tier-2 gating.

Prints literal counts/windows. Exit 0 always when the check runs cleanly;
exit 2 if the log looks unreadable/corrupt.

Usage:
  uv run python scripts/paper_data_gate.py
  uv run python scripts/paper_data_gate.py --log logs/paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _ts(obj: dict) -> float | None:
    for key in ("ts", "timestamp", "time"):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/paper.jsonl")
    ap.add_argument("--min-hours", type=float, default=24.0)
    ap.add_argument("--min-quotes", type=int, default=500)
    args = ap.parse_args()

    path = Path(args.log)
    now = datetime.now(timezone.utc).isoformat()
    print(f"paper_data_gate now={now}")
    print(f"log_path={path.resolve()}")

    if not path.exists():
        print("status=NO_LOG")
        print("runtime_hours=0")
        print("quote_events=0")
        print("requote_lines=0")
        print(f"tier2_allowed=false reason=no_log need_hours>={args.min_hours} need_quotes>={args.min_quotes}")
        return 0

    n_lines = 0
    n_json = 0
    n_bad = 0
    n_requote = 0
    times: list[float] = []
    try:
        with path.open() as fh:
            for line in fh:
                n_lines += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    n_bad += 1
                    continue
                n_json += 1
                event = str(obj.get("event") or obj.get("msg") or obj.get("event_name") or "")
                if event == "requote" or "requote" in event:
                    n_requote += 1
                t = _ts(obj)
                if t is not None:
                    times.append(t)
    except OSError as exc:
        print(f"status=CORRUPT err={exc}")
        return 2

    if n_bad > 0 and n_json == 0:
        print(f"status=CORRUPT bad_lines={n_bad} json_lines=0")
        return 2

    runtime_h = 0.0
    if len(times) >= 2:
        runtime_h = max(0.0, (max(times) - min(times)) / 3600.0)
    elif len(times) == 1:
        runtime_h = 0.0

    allowed = runtime_h >= args.min_hours and n_requote >= args.min_quotes
    print(f"status=OK lines={n_lines} json_lines={n_json} bad_lines={n_bad}")
    print(f"runtime_hours={runtime_h:.4f}")
    print(f"requote_lines={n_requote}")
    print(f"tier2_allowed={str(allowed).lower()} need_hours>={args.min_hours} need_quotes>={args.min_quotes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
