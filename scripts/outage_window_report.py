#!/usr/bin/env python3
"""Summarize collector/connectivity outage windows from strategy_cycles.jsonl.

Tier-1 ops view while Polymarket REST/WS is down and paper health is STALE.
Does not change strategy math.

Usage:
  uv run python scripts/outage_window_report.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_ts(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _f(row: dict[str, Any], *keys: str) -> float | None:
    cur: Any = row
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if cur is None or cur == "":
        return None
    try:
        return float(str(cur).split()[0])
    except ValueError:
        return None


def analyze_cycles(rows: list[dict[str, Any]]) -> dict[str, Any]:
    windows: list[dict[str, Any]] = []
    open_win: dict[str, Any] | None = None

    def close(end_row: dict[str, Any] | None) -> None:
        nonlocal open_win
        if open_win is None:
            return
        t0 = open_win["t_start"]
        t1 = _parse_ts((end_row or {}).get("ts")) if end_row else open_win.get("t_last")
        if t1 is None:
            t1 = open_win.get("t_last") or t0
        open_win["t_end"] = t1
        open_win["duration_s"] = round(max(0.0, float(t1) - float(t0)), 1)
        open_win["runtime_hours_end"] = _f(end_row or {}, "gate", "runtime_hours") or open_win.get(
            "runtime_hours_last"
        )
        open_win["quotes_end"] = _f(end_row or {}, "gate", "quotes_for_gate") or open_win.get(
            "quotes_last"
        )
        windows.append(open_win)
        open_win = None

    for row in rows:
        health = str((row.get("health") or {}).get("status") or "")
        conn = str((row.get("connectivity") or {}).get("status") or "")
        # STALE health or explicit DOWN/DEGRADED connectivity counts as outage.
        in_outage = health.upper() == "STALE" or conn.upper() in {"DOWN", "DEGRADED"}
        ts = _parse_ts(row.get("ts"))
        if in_outage:
            if open_win is None:
                open_win = {
                    "t_start": ts,
                    "t_last": ts,
                    "health_start": health or None,
                    "connectivity_start": conn or None,
                    "runtime_hours_start": _f(row, "gate", "runtime_hours"),
                    "quotes_start": _f(row, "gate", "quotes_for_gate"),
                    "runtime_hours_last": _f(row, "gate", "runtime_hours"),
                    "quotes_last": _f(row, "gate", "quotes_for_gate"),
                    "n_cycles": 1,
                }
            else:
                open_win["t_last"] = ts
                open_win["runtime_hours_last"] = _f(row, "gate", "runtime_hours")
                open_win["quotes_last"] = _f(row, "gate", "quotes_for_gate")
                open_win["n_cycles"] += 1
                open_win["health_last"] = health or None
                open_win["connectivity_last"] = conn or None
        else:
            if open_win is not None:
                close(row)

    open_now = open_win is not None
    if open_win is not None:
        # Still open — close against "now" for duration, keep open flag.
        now = datetime.now(timezone.utc).timestamp()
        open_win["t_last"] = open_win.get("t_last") or now
        open_win["open"] = True
        t0 = open_win["t_start"] or now
        open_win["duration_s"] = round(max(0.0, now - float(t0)), 1)
        open_win["runtime_hours_end"] = open_win.get("runtime_hours_last")
        open_win["quotes_end"] = open_win.get("quotes_last")
        windows.append(open_win)

    total_s = sum(float(w.get("duration_s") or 0.0) for w in windows)
    last = windows[-1] if windows else None
    return {
        "n_cycles": len(rows),
        "n_outage_windows": len(windows),
        "outage_open": open_now,
        "outage_total_s": round(total_s, 1),
        "outage_total_h": round(total_s / 3600.0, 4),
        "current": last if open_now else None,
        "windows": windows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default="logs/strategy_cycles.jsonl")
    args = ap.parse_args()
    path = Path(args.log)
    if not path.exists():
        print("status=NO_LOG", file=sys.stderr)
        return 2

    rows: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        print("status=EMPTY", file=sys.stderr)
        return 2

    rep = analyze_cycles(rows)
    print(json.dumps(rep, indent=2, sort_keys=True))
    cur = rep.get("current") or {}
    print(
        f"status=OK windows={rep['n_outage_windows']} open={rep['outage_open']} "
        f"total_h={rep['outage_total_h']} "
        f"current_duration_s={cur.get('duration_s')} "
        f"runtime_h={cur.get('runtime_hours_end')} quotes={cur.get('quotes_end')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
