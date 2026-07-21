#!/usr/bin/env python3
"""Replay a Journal JSONL through the strategy stack → metrics JSONL (T1-02).

No live connection. Output is analyzable with scripts/paper_metrics.py.

Usage:
  uv run python scripts/replay_backtest.py --journal path/to/paper.jsonl \\
      --metrics /tmp/metrics-replay.jsonl
  uv run python scripts/paper_metrics.py --log /tmp/metrics-replay.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics.analyze import analyze
from polymaker.replay import run_replay


def _default_meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xreplay",
        question="replay fixture",
        slug="replay-fixture",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
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
    ap.add_argument("--journal", required=True, help="Journal JSONL (kind/ts/data)")
    ap.add_argument("--metrics", default="logs/metrics-replay.jsonl")
    ap.add_argument("--yes-token", default="yes-token")
    ap.add_argument("--no-token", default="no-token")
    ap.add_argument("--condition-id", default="0xreplay")
    ap.add_argument("--tick-size", type=float, default=0.01)
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=NO_JOURNAL path={journal}", file=sys.stderr)
        return 2

    meta = _default_meta()
    meta = MarketMeta(
        condition_id=args.condition_id,
        question=meta.question,
        slug=meta.slug,
        tokens=(TokenMeta(args.yes_token, "Yes"), TokenMeta(args.no_token, "No")),
        tick_size=args.tick_size,
        neg_risk=meta.neg_risk,
        min_order_size=meta.min_order_size,
        rewards_min_size=meta.rewards_min_size,
        rewards_max_spread=meta.rewards_max_spread,
        rewards_daily_rate=meta.rewards_daily_rate,
        maker_fee_bps=meta.maker_fee_bps,
        taker_fee_bps=meta.taker_fee_bps,
        fees_enabled=meta.fees_enabled,
        end_date_iso=meta.end_date_iso,
        event_id=meta.event_id,
        rebate_rate=meta.rebate_rate,
    )
    metrics_path = Path(args.metrics)
    result = run_replay(journal, meta, StrategyProfile(), metrics_path)
    print(json.dumps({
        "events_read": result.events_read,
        "events_applied": result.events_applied,
        "recomputes": result.recomputes,
        "n_quote": result.n_quote,
        "n_cancel": result.n_cancel,
        "n_mark": result.n_mark,
        "metrics_path": result.metrics_path,
    }, indent=2, sort_keys=True))
    rep = analyze(metrics_path)
    print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    print(
        f"status=OK recomputes={result.recomputes} quotes={result.n_quote} "
        f"realized_spread_usdc={rep.realized_spread_usdc:.6f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
