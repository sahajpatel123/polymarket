#!/usr/bin/env python3
"""Sweep quote-distance knobs for tape touchability (n_crossable / bid gap).

Does not change defaults — diagnostic only. Goal: find overrides that make
resting bids crossable by sell aggressors so AS EV can bind later.

Usage:
  uv run python scripts/touchability_sweep.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --config-dir livecfg --baseline-profile live_scaled \\
      --yes-token … --no-token …
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Any

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.replay.compare import load_named_profile, profile_from_overrides
from polymaker.replay.quote_trade_gap import measure_quote_trade_gap


def _meta(args: argparse.Namespace) -> MarketMeta:
    return MarketMeta(
        condition_id=args.condition_id or "0xreplay",
        question="touch",
        slug="touch",
        tokens=(
            TokenMeta(args.yes_token or "yes-token", "Yes"),
            TokenMeta(args.no_token or "no-token", "No"),
        ),
        tick_size=args.tick_size or 0.001,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--baseline-profile", default="live_scaled")
    ap.add_argument("--yes-token", default=None)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument(
        "--delta-min-ticks",
        default="0,1,2",
        help="Comma list of delta_min_ticks candidates",
    )
    ap.add_argument("--c-vol", default="0.5,1.0,1.5")
    ap.add_argument("--min-edge-ticks", default="0,1")
    ap.add_argument("--report", default="logs/touchability_sweep/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    baseline = load_named_profile(args.baseline_profile, config_dir=args.config_dir)
    meta = _meta(args)
    rows = load_journal(journal)
    yes = args.yes_token or "yes-token"
    no = args.no_token or "no-token"
    if yes != "yes-token":
        filtered = filter_rows_for_tokens(rows, yes_token=yes, no_token=no)
        if filtered:
            rows = filtered

    deltas = [int(x) for x in args.delta_min_ticks.split(",") if x.strip()]
    cvols = [float(x) for x in args.c_vol.split(",") if x.strip()]
    edges = [int(x) for x in args.min_edge_ticks.split(",") if x.strip()]

    rows_out: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    for d_min, c_vol, edge in itertools.product(deltas, cvols, edges):
        ov = {
            "delta_min_ticks": d_min,
            "c_vol": c_vol,
            "min_edge_ticks": edge,
        }
        cand = profile_from_overrides(baseline, ov)
        gap = measure_quote_trade_gap(rows, meta, cand)
        g = gap.as_dict()
        row = {
            **ov,
            "n_crossable": g["n_crossable"],
            "n_fill": g["n_fill"],
            "n_quote": g["n_quote"],
            "n_trades_with_live": g["n_trades_with_live"],
            "median_bid_gap": g["median_bid_gap"],
            "mean_bid_gap": g["mean_bid_gap"],
            "mean_trade_minus_fv": g.get("mean_trade_minus_fv"),
            "reason": g["reason"],
            "is_baseline": (
                d_min == int(baseline.delta_min_ticks)
                and abs(c_vol - float(baseline.c_vol)) < 1e-12
                and edge == int(baseline.min_edge_ticks)
            ),
        }
        rows_out.append(row)
        print(
            f"delta_min={d_min} c_vol={c_vol} min_edge={edge} "
            f"crossable={row['n_crossable']} fill={row['n_fill']} "
            f"mean_gap={row['mean_bid_gap']} reason={row['reason']}"
        )
        if best is None or row["n_crossable"] > best["n_crossable"]:
            best = row
        elif (
            best is not None
            and row["n_crossable"] == best["n_crossable"]
            and (row["mean_bid_gap"] or 9e9) < (best["mean_bid_gap"] or 9e9)
        ):
            best = row

    report = {
        "baseline": {
            "delta_min_ticks": baseline.delta_min_ticks,
            "c_vol": baseline.c_vol,
            "min_edge_ticks": baseline.min_edge_ticks,
        },
        "best": best,
        "any_crossable": any(r["n_crossable"] > 0 for r in rows_out),
        "note": (
            "Diagnostic only — do not change defaults from this sweep alone. "
            "AS EV still requires as_ev_ready + conservative finding."
        ),
        "rows": rows_out,
    }
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"status=OK any_crossable={report['any_crossable']} "
        f"best_crossable={(best or {}).get('n_crossable')} "
        f"best={best} report={path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
