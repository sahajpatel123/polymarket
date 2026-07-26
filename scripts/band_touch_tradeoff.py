#!/usr/bin/env python3
"""Sweep rewards_max_spread: touchability vs in-band (reward) quoting.

Diagnostic only — does not change live defaults. Narrower bands pull bids
toward mid/touch (more crossable) but shrink the reward-eligible region.

Usage:
  uv run python scripts/band_touch_tradeoff.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --slug will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \\
      --db livecfg/state.db --config-dir livecfg
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from polymaker.replay import filter_rows_for_tokens, load_journal, run_replay
from polymaker.replay.compare import load_named_profile, write_sliced_journal
from polymaker.replay.market_resolve import resolve_market_by_slug
from polymaker.replay.quote_trade_gap import measure_quote_trade_gap


def _in_band_frac(metrics_path: Path) -> tuple[float, int, int]:
    n = 0
    n_in = 0
    with metrics_path.open() as fh:
        for line in fh:
            e = json.loads(line)
            if e.get("event") != "quote":
                continue
            n += 1
            if e.get("in_reward_band"):
                n_in += 1
    return (n_in / n if n else 0.0), n_in, n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--db", default="livecfg/state.db")
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument(
        "--spreads",
        default="5.5,3.0,2.0,1.0,0.5",
        help="Comma list of rewards_max_spread values (percent points)",
    )
    ap.add_argument("--report", default="logs/band_touch_tradeoff/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    base_meta = resolve_market_by_slug(args.slug, db_path=args.db)
    profile = load_named_profile(args.profile, config_dir=args.config_dir)
    rows = filter_rows_for_tokens(
        load_journal(journal),
        yes_token=base_meta.yes.token_id,
        no_token=base_meta.no.token_id,
    )
    spreads = [float(x) for x in args.spreads.split(",") if x.strip()]
    out_rows: list[dict[str, Any]] = []

    for spread in spreads:
        meta = replace(base_meta, rewards_max_spread=spread)
        gap = measure_quote_trade_gap(rows, meta, profile)
        g = gap.as_dict()
        with tempfile.TemporaryDirectory(prefix=f"band_{spread}_") as td:
            root = Path(td)
            jpath = write_sliced_journal(rows, root / "j.jsonl")
            mpath = root / "m.jsonl"
            result = run_replay(jpath, meta, profile, mpath, fill_mode="optimistic")
            in_frac, n_in, n_q = _in_band_frac(mpath)
        row = {
            "rewards_max_spread": spread,
            "is_catalog_default": abs(spread - float(base_meta.rewards_max_spread))
            < 1e-9,
            "n_crossable": g["n_crossable"],
            "n_fill_optimistic": result.n_fill,
            "n_quote": result.n_quote,
            "mean_bid_gap": g["mean_bid_gap"],
            "mean_mid_minus_bid": g["mean_mid_minus_bid"],
            "n_trades_with_live": g["n_trades_with_live"],
            "in_band_quote_frac": round(in_frac, 4),
            "n_quote_in_band": n_in,
            "n_quote_events": n_q,
            "gap_reason": g["reason"],
        }
        out_rows.append(row)
        print(
            f"spread={spread} crossable={row['n_crossable']} fill={row['n_fill_optimistic']} "
            f"mid_minus_bid={row['mean_mid_minus_bid']} in_band_frac={row['in_band_quote_frac']}"
        )

    any_cross = any(r["n_crossable"] > 0 for r in out_rows)
    best = max(out_rows, key=lambda r: (r["n_crossable"], -float(r["mean_mid_minus_bid"] or 9)))
    report = {
        "slug": args.slug,
        "catalog_rewards_max_spread": base_meta.rewards_max_spread,
        "yes_token": base_meta.yes.token_id,
        "no_token": base_meta.no.token_id,
        "any_crossable": any_cross,
        "best_crossable_row": best,
        "note": (
            "Diagnostic only. Narrower rewards_max_spread can increase tape "
            "touchability at the cost of in-band (reward) quote fraction. "
            "Do not change live market meta from this sweep alone."
        ),
        "rows": out_rows,
    }
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"status=OK any_crossable={any_cross} "
        f"best_spread={best['rewards_max_spread']} "
        f"best_crossable={best['n_crossable']} report={path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
