#!/usr/bin/env python3
"""Quote–trade gap: why optimistic fills are zero despite trades.

Usage:
  uv run python scripts/quote_trade_gap.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --config-dir livecfg --profile live_scaled \\
      --yes-token … --no-token …
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.replay.compare import load_named_profile
from polymaker.replay.quote_trade_gap import measure_quote_trade_gap


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument("--yes-token", default=None)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--report", default="logs/quote_trade_gap/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = MarketMeta(
        condition_id=args.condition_id or "0xreplay",
        question="gap",
        slug="gap",
        tokens=(
            TokenMeta(args.yes_token or "yes-token", "Yes"),
            TokenMeta(args.no_token or "no-token", "No"),
        ),
        tick_size=args.tick_size,
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
    profile = load_named_profile(args.profile, config_dir=args.config_dir)
    rows = load_journal(journal)
    yes = args.yes_token or "yes-token"
    no = args.no_token or "no-token"
    if yes != "yes-token":
        filtered = filter_rows_for_tokens(rows, yes_token=yes, no_token=no)
        if filtered:
            rows = filtered

    gap = measure_quote_trade_gap(rows, meta, profile)
    d = gap.as_dict()
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    print(
        f"status=OK reason={d['reason']} n_trades={d['n_trades']} "
        f"n_crossable={d['n_crossable']} n_fill={d['n_fill']} "
        f"median_bid_gap={d['median_bid_gap']} mean_bid_gap={d['mean_bid_gap']} "
        f"agg_buy={d['n_aggressor_buy']} agg_sell={d['n_aggressor_sell']} "
        f"report={path}"
    )
    return 0 if d["n_crossable"] > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
