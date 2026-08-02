#!/usr/bin/env python3
"""Measure REAL trade-print rate per market from the live WebSocket.

Why: Gamma's ``volume_24hr`` is a poor proxy for whether a market is trading
*now*. Markets showing $2.8M/24h produced zero prints over an 8-minute window,
so a maker exit resting at the touch had nothing to fill against. A maker
strategy cannot round-trip in a market that does not print, and no model change
fixes that — so market selection has to be driven by observed print rate.

Subscribes to the top-N candidate markets, counts prints for a fixed window,
and ranks them.

Usage:
    uv run python scripts/print_rate_recon.py --minutes 5 --top 40
    uv run python scripts/print_rate_recon.py --minutes 5 --json-out /tmp/rates.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from polymaker.marketdata.service import MarketDataService


def candidates(db: Path, top: int, min_vol: float) -> list[dict[str, Any]]:
    """Freshly-scanned, tradable markets ranked by 24h volume."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cutoff = time.time() - 3600
    out: list[dict[str, Any]] = []
    for row in con.execute(
        "SELECT slug, condition_id, meta_json FROM markets WHERE scanned_ts > ?",
        (cutoff,),
    ):
        try:
            m = json.loads(row["meta_json"] or "{}")
        except json.JSONDecodeError:
            continue
        vol = float(m.get("volume_24hr") or 0.0)
        if vol < min_vol:
            continue
        bid = float(m.get("best_bid") or 0.0)
        ask = float(m.get("best_ask") or 0.0)
        if not (0.02 < bid < 0.98) or ask <= bid:
            continue
        toks = [
            t.get("token_id")
            for t in (m.get("tokens") or [])
            if isinstance(t, dict) and t.get("token_id")
        ]
        if len(toks) < 2:
            continue
        out.append({
            "slug": row["slug"],
            "condition_id": row["condition_id"],
            "tokens": toks[:2],
            "vol24h": vol,
            "bid": bid,
            "ask": ask,
            "tick": float(m.get("tick_size") or 0.001),
            "min_size": float(m.get("rewards_min_size") or 0.0),
            "reward": float(m.get("rewards_daily_rate") or 0.0),
        })
    con.close()
    out.sort(key=lambda r: -r["vol24h"])
    return out[:top]


async def measure(cands: list[dict[str, Any]], seconds: float) -> Counter:
    prints: Counter = Counter()
    tok_slug = {t: c["slug"] for c in cands for t in c["tokens"]}

    def on_trade(tp: Any) -> None:
        prints[tok_slug.get(tp.asset_id, tp.asset_id)] += 1

    md = MarketDataService(on_trade=on_trade)
    md.set_markets([(c["condition_id"], c["tokens"]) for c in cands])
    task = asyncio.create_task(md.run())
    try:
        await asyncio.sleep(seconds)
    finally:
        md.stop()
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
            await task
    return prints


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("state.db"))
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-vol", type=float, default=20000.0)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    cands = candidates(args.db, args.top, args.min_vol)
    if not cands:
        raise SystemExit("no fresh candidates — run `polymaker scan` first")
    print(f"subscribing to {len(cands)} markets for {args.minutes:.1f} min ...")
    prints = asyncio.run(measure(cands, args.minutes * 60.0))

    per_min = args.minutes
    rows = []
    for c in cands:
        n = prints.get(c["slug"], 0)
        rows.append({**c, "prints": n, "prints_per_min": round(n / per_min, 3)})
    rows.sort(key=lambda r: (-r["prints"], -r["vol24h"]))

    total = sum(r["prints"] for r in rows)
    active = [r for r in rows if r["prints"] > 0]
    print(f"\ntotal prints: {total} across {len(active)}/{len(rows)} markets"
          f" in {args.minutes:.1f} min\n")
    print(f"{'p/min':>7} {'prints':>7} {'vol24h':>11} {'spr_t':>6} {'minsz':>6} slug")
    for r in rows[:25]:
        spr = (r["ask"] - r["bid"]) / max(r["tick"], 1e-9)
        print(f"{r['prints_per_min']:>7.2f} {r['prints']:>7} {r['vol24h']:>11,.0f} "
              f"{spr:>6.0f} {r['min_size']:>6.0f} {r['slug'][:48]}")
    if args.json_out:
        args.json_out.write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json_out}")
    if total == 0:
        print("\nNOTE: zero prints anywhere — either the feed is down or the "
              "whole candidate set is dormant. A maker cannot round-trip here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
