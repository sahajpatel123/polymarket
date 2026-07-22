#!/usr/bin/env python3
"""Check that paper collection is still advancing (staleness watchdog).

Tier-1 ops tool for long unattended paper runs toward the 24h Tier-2 gate.

Usage:
  uv run python scripts/paper_health.py
  uv run python scripts/paper_health.py --max-age-s 300
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from polymaker.metrics.log_discovery import (
    DEFAULT_METRICS_CANDIDATES,
    DEFAULT_PAPER_CANDIDATES,
    pick_richest_log,
)


def _parse_ts(obj: dict[str, Any]) -> float | None:
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


def _last_event_ts(path: Path, event: str | None = None) -> tuple[float | None, int]:
    last = None
    n = 0
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if event is not None:
                ev = str(obj.get("event") or obj.get("msg") or "")
                if ev != event:
                    continue
            t = _parse_ts(obj)
            if t is None:
                continue
            n += 1
            if last is None or t > last:
                last = t
    return last, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paper-log", default=None)
    ap.add_argument("--metrics-log", default=None)
    ap.add_argument("--max-age-s", type=float, default=300.0,
                    help="Fail if newest requote/quote is older than this (default 300s)")
    args = ap.parse_args()

    paper = Path(args.paper_log) if args.paper_log else pick_richest_log(
        DEFAULT_PAPER_CANDIDATES
    )
    metrics = Path(args.metrics_log) if args.metrics_log else pick_richest_log(
        DEFAULT_METRICS_CANDIDATES
    )
    now = datetime.now(timezone.utc).timestamp()
    report: dict[str, Any] = {
        "now": datetime.now(timezone.utc).isoformat(),
        "max_age_s": args.max_age_s,
    }

    if paper is None or not paper.exists():
        print("status=NO_PAPER_LOG", file=sys.stderr)
        print(json.dumps({"status": "NO_PAPER_LOG"}, indent=2))
        return 2
    if metrics is None or not metrics.exists():
        print("status=NO_METRICS", file=sys.stderr)
        print(json.dumps({"status": "NO_METRICS"}, indent=2))
        return 2

    req_ts, n_req = _last_event_ts(paper, "requote")
    quote_ts, n_quote = _last_event_ts(metrics, "quote")
    report.update({
        "paper_log": str(paper),
        "metrics_log": str(metrics),
        "paper_bytes": paper.stat().st_size,
        "metrics_bytes": metrics.stat().st_size,
        "n_requote": n_req,
        "n_quote": n_quote,
        "last_requote_age_s": None if req_ts is None else round(now - req_ts, 3),
        "last_quote_age_s": None if quote_ts is None else round(now - quote_ts, 3),
    })

    ages = [a for a in (report["last_requote_age_s"], report["last_quote_age_s"]) if a is not None]
    stale = (not ages) or min(ages) > args.max_age_s
    report["healthy"] = not stale
    print(json.dumps(report, indent=2, sort_keys=True))
    if stale:
        print(
            f"status=STALE last_requote_age_s={report['last_requote_age_s']} "
            f"last_quote_age_s={report['last_quote_age_s']} max_age_s={args.max_age_s}",
            file=sys.stderr,
        )
        return 1
    print(
        f"status=OK last_requote_age_s={report['last_requote_age_s']} "
        f"last_quote_age_s={report['last_quote_age_s']} "
        f"n_quote={n_quote} n_requote={n_req}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
