#!/usr/bin/env python3
"""Query paper-trading log coverage for Autonomous Loop Tier-2 gating.

Prints literal counts/windows. Exit 0 always when the check runs cleanly;
exit 2 if the log looks unreadable/corrupt.

Quote threshold follows BACKLOG wording (≥500 *quotes*): prefers
metrics-paper.jsonl `event=quote` counts when that sibling log exists, and
still reports structlog requote lines for churn context. Runtime comes from
the **requote** timeline (not blind/WS-error noise that continues during
outages — T1-52), unioned across active ``paper.jsonl`` + dated rotations
in the same directory (T1-98).

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

from polymaker.metrics.log_discovery import (
    DEFAULT_METRICS_CANDIDATES,
    DEFAULT_PAPER_CANDIDATES,
    paper_log_family,
    pick_richest_log,
    pick_richest_paper_family,
)


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


def _sibling_metrics(paper_log: Path) -> Path | None:
    """Prefer metrics beside the chosen paper log, else richest known metrics."""
    cand = paper_log.with_name("metrics-paper.jsonl")
    if cand.exists():
        return cand
    return pick_richest_log(DEFAULT_METRICS_CANDIDATES)


def _count_metric_quotes(path: Path) -> int:
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
            if isinstance(obj, dict) and obj.get("event") == "quote":
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=None,
                    help="Paper JSONL (default: richest among "
                         "livecfg/logs/paper.jsonl, logs/paper.jsonl)")
    ap.add_argument("--metrics-log", default=None,
                    help="Optional metrics JSONL for quote event counts")
    ap.add_argument("--min-hours", type=float, default=24.0)
    ap.add_argument("--min-quotes", type=int, default=500)
    args = ap.parse_args()

    if args.log:
        path = Path(args.log)
        # Explicit --log: still union same-dir rotations when path is paper.jsonl*
        # so midnight splits do not freeze gate progress (T1-98). Non-paper
        # names (fixtures) stay single-file.
        family = paper_log_family(path) if path.name.startswith("paper.jsonl") else [path]
    else:
        family = pick_richest_paper_family(DEFAULT_PAPER_CANDIDATES)
        if not family:
            path = Path(DEFAULT_PAPER_CANDIDATES[0])
            family = [path]
        else:
            # Primary display path = richest single member (T1-97); runtime
            # still unions the whole family (T1-98).
            path = pick_richest_log(family) or family[0]

    now = datetime.now(timezone.utc).isoformat()
    print(f"paper_data_gate now={now}")
    print(f"log_path={path.resolve()}")
    if len(family) > 1:
        print(f"log_files={len(family)}")
        print("log_paths=" + ",".join(str(p.resolve()) for p in family))

    if not any(p.exists() for p in family):
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
    times_all: list[float] = []
    times_requote: list[float] = []
    try:
        for member in family:
            if not member.exists():
                continue
            with member.open() as fh:
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
                    is_requote = event == "requote" or "requote" in event
                    if is_requote:
                        n_requote += 1
                    t = _ts(obj)
                    if t is not None:
                        times_all.append(t)
                        if is_requote:
                            times_requote.append(t)
    except OSError as exc:
        print(f"status=CORRUPT err={exc}")
        return 2

    if n_bad > 0 and n_json == 0:
        print(f"status=CORRUPT bad_lines={n_bad} json_lines=0")
        return 2

    def _span_h(times: list[float]) -> float:
        if len(times) >= 2:
            return max(0.0, (max(times) - min(times)) / 3600.0)
        return 0.0

    # Prefer requote span so outage noise (ws_dropped / get_full_book_failed)
    # cannot pad toward the 24h Tier-2 gate (T1-52).
    runtime_basis = "requote"
    runtime_h = _span_h(times_requote)
    runtime_h_all = _span_h(times_all)
    if runtime_h <= 0.0 and runtime_h_all > 0.0:
        # Legacy / empty-requote logs: fall back to full structlog span.
        runtime_basis = "all_events"
        runtime_h = runtime_h_all

    metrics_path = Path(args.metrics_log) if args.metrics_log else _sibling_metrics(path)
    quote_events = 0
    if metrics_path is not None and metrics_path.exists():
        quote_events = _count_metric_quotes(metrics_path)
        print(f"metrics_path={metrics_path.resolve()}")
    # BACKLOG: ≥500 quotes. Prefer metrics quote events; fall back to requotes.
    quotes_for_gate = quote_events if quote_events > 0 else n_requote

    hours_ok = runtime_h >= args.min_hours
    quotes_ok = quotes_for_gate >= args.min_quotes
    allowed = hours_ok and quotes_ok
    reason_parts = []
    if not hours_ok:
        reason_parts.append(f"need_hours>={args.min_hours}")
    if not quotes_ok:
        reason_parts.append(f"need_quotes>={args.min_quotes}")
    reason = " ".join(reason_parts) if reason_parts else "ok"

    print(f"status=OK lines={n_lines} json_lines={n_json} bad_lines={n_bad}")
    print(f"runtime_basis={runtime_basis}")
    print(f"runtime_hours={runtime_h:.4f}")
    print(f"runtime_hours_all_events={runtime_h_all:.4f}")
    print(f"quote_events={quote_events}")
    print(f"requote_lines={n_requote}")
    print(f"quotes_for_gate={quotes_for_gate}")
    print(f"tier2_allowed={str(allowed).lower()} reason={reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
