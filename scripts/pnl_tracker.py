#!/usr/bin/env python3
"""Track PnL and strategy metrics over time from metrics JSONL + state DB.

Reads the latest metrics log (paper or live) and the SQLite state to produce
a daily/hourly PnL summary. Useful for unattended operation monitoring.

Usage:
  uv run python scripts/pnl_tracker.py --metrics logs/metrics-paper.jsonl --out pnl_history.json
  uv run python scripts/pnl_tracker.py --metrics logs/metrics-live.jsonl --out pnl_history.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def compute_daily_pnl(events: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Bucket PnL by day (UTC) and per-market.

    PnL components:
    - spread_pnl: realized maker edge from fills (vs contemporaneous mid)
    - reward_pnl: accrued liquidity rewards (time-in-band * daily rate)
    - fill_count: number of fills
    - quote_count: number of quotes placed
    - cancel_count: number of cancels
    """
    daily: dict[str, dict[str, float]] = defaultdict(lambda: {
        "spread_pnl": 0.0,
        "reward_pnl": 0.0,
        "fill_count": 0,
        "quote_count": 0,
        "cancel_count": 0,
        "mark_count": 0,
    })

    # First pass: collect fills and quotes
    for e in events:
        ev = str(e.get("event", ""))
        ts = float(e.get("ts") or 0.0)
        if ts <= 0:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))

        if ev == "fill":
            price = float(e.get("price") or 0.0)
            size = float(e.get("size") or 0.0)
            side = str(e.get("side") or "")
            mid = e.get("mid")
            if mid is None:
                mid = e.get("fv")
            if mid is not None and size > 0:
                m = float(mid)
                if side == "BUY":
                    pnl = (m - price) * size
                elif side == "SELL":
                    pnl = (price - m) * size
                else:
                    pnl = 0.0
                daily[day]["spread_pnl"] += pnl
            daily[day]["fill_count"] += 1

        elif ev == "quote":
            daily[day]["quote_count"] += 1
            in_band = bool(e.get("in_reward_band", False))
            if in_band and "rewards_daily_rate" in e:
                # rough: add reward for in-band quote (will be refined in second pass)
                pass

        elif ev == "cancel":
            daily[day]["cancel_count"] += 1

        elif ev == "mark":
            daily[day]["mark_count"] += 1

    # Second pass: compute reward PnL (time-in-band * daily rate)
    # Group quotes by market and day, compute time-in-band
    market_quotes: dict[str, dict[str, list[tuple[float, bool, float]]]] = defaultdict(lambda: defaultdict(list))
    market_meta: dict[str, dict[str, Any]] = {}
    for e in events:
        ev = str(e.get("event", ""))
        if ev == "market_meta":
            cid = str(e.get("condition_id") or "")
            market_meta[cid] = e
        elif ev == "quote":
            cid = str(e.get("condition_id") or "")
            ts = float(e.get("ts") or 0.0)
            in_band = bool(e.get("in_reward_band", False))
            day = time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts > 0 else "unknown"
            market_quotes[cid][day].append((ts, in_band, 0.0))

    for cid, days in market_quotes.items():
        meta = market_meta.get(cid, {})
        daily_rate = float(meta.get("rewards_daily_rate") or 0.0)
        if daily_rate <= 0:
            continue
        for day, quotes in days.items():
            quotes.sort()
            in_band_s = 0.0
            for (t0, b0, _), (t1, _, _) in zip(quotes, quotes[1:], strict=False):
                if b0:
                    in_band_s += max(0.0, t1 - t0)
            reward = daily_rate * (in_band_s / 86400.0)
            daily[day]["reward_pnl"] += reward

    return dict(daily)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metrics", required=True, help="path to metrics JSONL")
    ap.add_argument("--out", default="pnl_history.json", help="output JSON path")
    ap.add_argument("--append", action="store_true", help="append to existing output (preserve history)")
    args = ap.parse_args()

    metrics_path = Path(args.metrics)
    if not metrics_path.exists():
        print(f"ERROR: metrics file not found: {metrics_path}", file=sys.stderr)
        return 1

    events = load_events(metrics_path)
    print(f"Loaded {len(events)} events from {metrics_path}")

    daily = compute_daily_pnl(events)

    if not daily:
        print("No events to summarize.", file=sys.stderr)
        return 1

    # Sort by day
    days = sorted(daily.keys())
    print("\nDaily PnL Summary:")
    print(f"{'Day':<12} {'Spread':>10} {'Reward':>10} {'Total':>10} {'Fills':>6} {'Quotes':>7} {'Cancels':>7}")
    print("-" * 72)

    totals = {"spread": 0.0, "reward": 0.0, "fills": 0, "quotes": 0, "cancels": 0}
    for day in days:
        d = daily[day]
        total = d["spread_pnl"] + d["reward_pnl"]
        print(f"{day:<12} {d['spread_pnl']:>10.4f} {d['reward_pnl']:>10.4f} {total:>10.4f} "
              f"{int(d['fill_count']):>6} {int(d['quote_count']):>7} {int(d['cancel_count']):>7}")
        totals["spread"] += d["spread_pnl"]
        totals["reward"] += d["reward_pnl"]
        totals["fills"] += int(d["fill_count"])
        totals["quotes"] += int(d["quote_count"])
        totals["cancels"] += int(d["cancel_count"])

    grand_total = totals["spread"] + totals["reward"]
    print("-" * 72)
    print(f"{'TOTAL':<12} {totals['spread']:>10.4f} {totals['reward']:>10.4f} {grand_total:>10.4f} "
          f"{totals['fills']:>6} {totals['quotes']:>7} {totals['cancels']:>7}")

    # Write output
    out_path = Path(args.out)
    if args.append and out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except json.JSONDecodeError:
            existing = {"history": []}
    else:
        existing = {"history": []}

    entry = {
        "ts": time.time(),
        "metrics_path": str(metrics_path),
        "n_events": len(events),
        "n_days": len(days),
        "daily": daily,
        "totals": {
            "spread_usdc": round(totals["spread"], 6),
            "reward_usdc": round(totals["reward"], 6),
            "total_usdc": round(grand_total, 6),
            "fills": totals["fills"],
            "quotes": totals["quotes"],
            "cancels": totals["cancels"],
        },
    }
    existing["history"].append(entry)
    out_path.write_text(json.dumps(existing, indent=2, default=str))
    print(f"\nWrote PnL history to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
