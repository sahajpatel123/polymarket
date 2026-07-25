#!/usr/bin/env python3
"""Champion–challenger harness: baseline_naive vs profile challengers.

Runs the same journal through multiple StrategyProfiles and reports
paired counts + equity. A challenger is *not* promoted here — this only
produces the comparison table. Promotion requires locked holdout + CI.

Usage:
  uv run python scripts/champion_challenger.py --journal path.jsonl \\
      --yes-token ... --no-token ... --condition-id ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polymaker.benchmark import ValidityConfig, evaluate_benchmark
from polymaker.config import Config, StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay import run_replay


def _meta(args: argparse.Namespace) -> MarketMeta:
    return MarketMeta(
        condition_id=args.condition_id,
        question=args.question,
        slug=args.slug,
        tokens=(
            TokenMeta(args.yes_token, "Yes"),
            TokenMeta(args.no_token, "No"),
        ),
        tick_size=args.tick_size,
        neg_risk=False,
        min_order_size=args.min_order_size,
        rewards_min_size=args.rewards_min_size,
        rewards_max_spread=args.rewards_max_spread,
        rewards_daily_rate=args.rewards_daily_rate,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-01-01T00:00:00Z",
        event_id="cc",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", required=True)
    ap.add_argument("--condition-id", default="0xcc")
    ap.add_argument("--slug", default="champion-challenger")
    ap.add_argument("--question", default="cc")
    ap.add_argument("--tick-size", type=float, default=0.01)
    ap.add_argument("--min-order-size", type=float, default=5.0)
    ap.add_argument("--rewards-min-size", type=float, default=10.0)
    ap.add_argument("--rewards-max-spread", type=float, default=3.0)
    ap.add_argument("--rewards-daily-rate", type=float, default=50.0)
    ap.add_argument("--config-dir", default="config")
    ap.add_argument(
        "--challengers",
        default="political-hot,live_scaled,baseline_naive",
        help="Comma-separated profile names (baseline_naive is champion)",
    )
    ap.add_argument("--out", type=Path, default=Path("champion_challenger_report.json"))
    args = ap.parse_args()

    cfg = Config.load(args.config_dir)
    meta = _meta(args)
    names = [n.strip() for n in args.challengers.split(",") if n.strip()]
    if "baseline_naive" not in names:
        names.insert(0, "baseline_naive")

    results = []
    for name in names:
        profile = cfg.profiles.get(name) or StrategyProfile()
        if name in cfg.profiles:
            profile = cfg.profiles[name]
        mpath = args.out.parent / f"cc_{name}_metrics.jsonl"
        rr = run_replay(args.journal, meta, profile, mpath, strict_sync=True)
        validity = evaluate_benchmark(
            n_quote=rr.n_quote,
            n_fill=rr.n_fill,
            n_mark=rr.n_mark,
            n_markets=1,
            runtime_s=1.0,
            n_trade_prints=rr.events_applied,
            state_divergence_events=rr.state_divergence_events,
            fills_after_cancel=rr.fills_after_cancel,
            overfills=rr.overfills,
            cfg=ValidityConfig(
                min_quotes=1,
                min_fills=0,
                min_marks=1,
                min_runtime_s=0,
                min_trade_prints=0,
                require_actionable_quotes=False,
            ),
        )
        row = {
            "profile": name,
            "champion": name == "baseline_naive",
            "n_quote": rr.n_quote,
            "n_fill": rr.n_fill,
            "n_mark": rr.n_mark,
            "n_cancel": rr.n_cancel,
            "equity": rr.final_equity,
            "cash": rr.final_cash,
            "state_divergence_events": rr.state_divergence_events,
            "fills_after_cancel": rr.fills_after_cancel,
            "overfills": rr.overfills,
            "validity": validity.as_dict(),
        }
        results.append(row)
        print(
            f"{name:20s} quotes={rr.n_quote:5d} fills={rr.n_fill:4d} "
            f"equity={rr.final_equity:+.4f} validity={validity.status.value}"
        )

    report = {
        "champion": "baseline_naive",
        "journal": str(args.journal),
        "results": results,
        "note": (
            "Promotion requires locked holdout, conservative fills, "
            "paired CI of risk-adjusted delta > 0 — this script only compares."
        ),
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
