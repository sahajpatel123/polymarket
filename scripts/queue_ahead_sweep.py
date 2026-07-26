#!/usr/bin/env python3
"""Diagnose why join-touch fills vanish under conservative fill mode.

Sweeps fill_mode × queue_ahead for a join+min_edge0 profile and reports
n_fill / n_queue_blocked. Separately counts equal-price vs through-price
optimistic fills (conservative skips equal-price by design).

Diagnostic only — does not change live defaults.

Usage:
  uv run python scripts/queue_ahead_sweep.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --slug will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \\
      --db livecfg/state.db --config-dir livecfg
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from polymaker.replay import filter_rows_for_tokens, load_journal, run_replay
from polymaker.replay.compare import (
    load_named_profile,
    profile_from_overrides,
    write_sliced_journal,
)
from polymaker.replay.market_resolve import resolve_market_by_slug


def _classify_optimistic_fills(metrics_path: Path) -> dict[str, Any]:
    """Count fill events vs nearest prior live bid at same token (equal vs through)."""
    # Simpler: metrics emit fill events with price; quote events with price.
    # Classify fill price relative to trade print is not stored; use fill vs
    # last quote price for that order_id when available.
    quotes: dict[str, float] = {}
    n_fill = 0
    n_equal = 0
    n_through = 0
    n_unknown = 0
    with metrics_path.open() as fh:
        for line in fh:
            e = json.loads(line)
            ev = e.get("event")
            if ev == "quote":
                oid = str(e.get("order_id") or "")
                if oid:
                    quotes[oid] = float(e.get("price") or 0.0)
            elif ev == "fill":
                n_fill += 1
                oid = str(e.get("order_id") or "")
                px = float(e.get("price") or 0.0)
                # Trade aggressor sell at fill price for maker bid: equal if
                # metrics store trade_price; else compare is trivial (fill at quote).
                trade_px = e.get("trade_price")
                if trade_px is None:
                    # Fallback: maker fill price == resting quote ⇒ equal-price
                    # aggressors that hit without improving. Through would require
                    # trade below bid; optimistic FillSimulator fills at order price.
                    qpx = quotes.get(oid)
                    if qpx is None:
                        n_unknown += 1
                    elif abs(px - qpx) < 1e-12:
                        n_equal += 1  # filled at resting bid (typical join-BB)
                    else:
                        n_through += 1
                else:
                    tpx = float(trade_px)
                    if abs(tpx - px) < 1e-12:
                        n_equal += 1
                    elif tpx < px - 1e-12:  # sell through our bid
                        n_through += 1
                    else:
                        n_unknown += 1
    return {
        "n_fill": n_fill,
        "n_equal_price": n_equal,
        "n_through_price": n_through,
        "n_unknown": n_unknown,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--db", default="livecfg/state.db")
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument(
        "--overrides",
        default='{"join_best_bid": true, "min_edge_ticks": 0}',
        help="JSON StrategyProfile overrides (default: join+min_edge0)",
    )
    ap.add_argument("--report", default="logs/queue_ahead_sweep/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = resolve_market_by_slug(args.slug, db_path=args.db)
    base = load_named_profile(args.profile, config_dir=args.config_dir)
    ov = json.loads(args.overrides)
    profile = profile_from_overrides(base, ov)
    rows = filter_rows_for_tokens(
        load_journal(journal),
        yes_token=meta.yes.token_id,
        no_token=meta.no.token_id,
    )

    # (label, fill_mode, queue_ahead|None)
    grid: list[tuple[str, str, float | None]] = [
        ("optimistic", "optimistic", None),
        ("base_ahead0", "base", 0.0),
        ("base_default50", "base", None),
        ("cons_ahead0", "conservative", 0.0),
        ("cons_ahead10", "conservative", 10.0),
        ("cons_ahead50", "conservative", 50.0),
        ("cons_default200", "conservative", None),
    ]

    out_rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="qahead_") as td:
        root = Path(td)
        jpath = write_sliced_journal(rows, root / "j.jsonl")
        for label, mode, ahead in grid:
            mpath = root / f"{label}.jsonl"
            rr = run_replay(
                jpath, meta, profile, mpath, fill_mode=mode, queue_ahead=ahead
            )
            row: dict[str, Any] = {
                "label": label,
                "fill_mode": mode,
                "queue_ahead": ahead if ahead is not None else "mode_default",
                "n_fill": rr.n_fill,
                "n_quote": rr.n_quote,
                "n_queue_blocked": rr.n_queue_blocked,
                "n_latency_blocked": rr.n_latency_blocked,
            }
            if mode == "optimistic":
                row["fill_price_class"] = _classify_optimistic_fills(mpath)
            out_rows.append(row)
            print(
                f"{label}: n_fill={rr.n_fill} n_queue_blocked={rr.n_queue_blocked} "
                f"n_latency_blocked={rr.n_latency_blocked}"
            )

    # Equal-price smoking gun: cons_ahead0 vs base_ahead0
    by_label = {r["label"]: r for r in out_rows}
    note = (
        "If base_ahead0 fills but cons_ahead0 does not, conservative equal-price "
        "skip (not queue depth) blocks join-BB fills. If both zero until "
        "optimistic, price-touchability is still the issue."
    )
    report = {
        "slug": args.slug,
        "overrides": ov,
        "note": note,
        "rows": out_rows,
        "equal_price_blocks_join": (
            by_label.get("base_ahead0", {}).get("n_fill", 0) > 0
            and by_label.get("cons_ahead0", {}).get("n_fill", 0) == 0
        ),
        "queue_blocks_after_equal_ok": (
            by_label.get("base_ahead0", {}).get("n_fill", 0) > 0
            and by_label.get("cons_default200", {}).get("n_fill", 0) == 0
        ),
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"status=OK report={out} equal_price_blocks_join={report['equal_price_blocks_join']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
