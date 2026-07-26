#!/usr/bin/env python3
"""Report whether a journal can support adverse-selection EV claims.

Usage:
  uv run python scripts/fill_readiness.py \\
      --journal livecfg/journal/paper.jsonl \\
      --yes-token ... --no-token ... \\
      --probe-optimistic --config-dir livecfg --profile live_scaled
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import load_named_profile
from polymaker.replay.fill_readiness import assess_fill_readiness, write_fill_readiness


def _meta(args: argparse.Namespace) -> MarketMeta:
    return MarketMeta(
        condition_id=args.condition_id or "0xreplay",
        question="fill-readiness",
        slug="fill-readiness",
        tokens=(
            TokenMeta(args.yes_token or "yes-token", "Yes"),
            TokenMeta(args.no_token or "no-token", "No"),
        ),
        tick_size=args.tick_size or 0.01,
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
    ap.add_argument("--yes-token", default=None)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--min-trades", type=int, default=50)
    ap.add_argument("--min-fills-optimistic", type=int, default=20)
    ap.add_argument("--probe-optimistic", action="store_true")
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument("--report", default="logs/fill_readiness/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = _meta(args)
    profile = None
    if args.probe_optimistic:
        profile = load_named_profile(args.profile, config_dir=args.config_dir)

    report = assess_fill_readiness(
        journal,
        meta,
        profile=profile,
        min_trades=args.min_trades,
        min_fills_optimistic=args.min_fills_optimistic,
        run_optimistic_probe=args.probe_optimistic,
    )
    path = write_fill_readiness(report, Path(args.report))
    d = report.as_dict()
    print(
        f"status=OK as_ev_ready={d['as_ev_ready']} reason={d['reason']} "
        f"n_trades={d['n_trades']} n_fill_optimistic={d['n_fill_optimistic']} "
        f"trades_per_hour={d['trades_per_hour']} report={path}"
    )
    return 0 if d["as_ev_ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
