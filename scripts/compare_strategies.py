#!/usr/bin/env python3
"""A/B compare two StrategyProfiles on one journal (strategy eval harness).

Replays the same book tape through baseline vs candidate knobs and prints
T1-01 metric deltas. Does not modify strategy code.

Usage:
  uv run python scripts/compare_strategies.py \\
      --journal path/to/paper.jsonl \\
      --candidate-overrides '{"gamma": 0.8, "c_tox": 4.0}'

  # Named profiles from strategy.toml:
  uv run python scripts/compare_strategies.py --journal j.jsonl \\
      --baseline-profile newsom-mm --candidate-profile romania-pm

  # Score only the last 30% of the timeline (OOS holdout):
  uv run python scripts/compare_strategies.py --journal j.jsonl \\
      --candidate-overrides '{"reprice_ticks": 3}' \\
      --holdout-frac 0.3 --use-holdout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import (
    compare_profiles,
    load_named_profile,
    profile_from_overrides,
)


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


def _load_overrides(raw: str | None, path: str | None) -> dict:
    if path:
        text = Path(path).read_text()
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise SystemExit(f"overrides file must be a JSON object: {path}")
        return obj
    if raw:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise SystemExit("--*-overrides must be a JSON object")
        return obj
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True, help="Journal JSONL (kind/ts/data)")
    ap.add_argument("--out-dir", default="logs/compare_strategies")
    ap.add_argument("--config-dir", default="config",
                    help="Config dir for named --*-profile lookup")
    ap.add_argument("--baseline-profile", default=None,
                    help="Named StrategyProfile from strategy.toml (else defaults)")
    ap.add_argument("--candidate-profile", default=None,
                    help="Named StrategyProfile from strategy.toml")
    ap.add_argument("--baseline-overrides", default=None,
                    help="JSON object of StrategyProfile field overrides")
    ap.add_argument("--baseline-overrides-file", default=None)
    ap.add_argument("--candidate-overrides", default=None,
                    help="JSON object of StrategyProfile field overrides")
    ap.add_argument("--candidate-overrides-file", default=None)
    ap.add_argument("--holdout-frac", type=float, default=0.0,
                    help="Fraction of timeline reserved as holdout (0 = full window)")
    ap.add_argument("--use-holdout", action="store_true",
                    help="Evaluate on holdout slice instead of tune slice")
    ap.add_argument("--yes-token", default="yes-token")
    ap.add_argument("--no-token", default="no-token")
    ap.add_argument("--condition-id", default="0xreplay")
    ap.add_argument("--tick-size", type=float, default=0.01)
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=NO_JOURNAL path={journal}", file=sys.stderr)
        return 2

    base_over = _load_overrides(args.baseline_overrides, args.baseline_overrides_file)
    cand_over = _load_overrides(args.candidate_overrides, args.candidate_overrides_file)
    if (
        not cand_over
        and not args.candidate_overrides_file
        and not args.candidate_profile
    ):
        print(
            "status=NEED_CANDIDATE provide --candidate-profile, "
            "--candidate-overrides, or --candidate-overrides-file",
            file=sys.stderr,
        )
        return 2

    meta0 = _default_meta()
    meta = MarketMeta(
        condition_id=args.condition_id,
        question=meta0.question,
        slug=meta0.slug,
        tokens=(TokenMeta(args.yes_token, "Yes"), TokenMeta(args.no_token, "No")),
        tick_size=args.tick_size,
        neg_risk=meta0.neg_risk,
        min_order_size=meta0.min_order_size,
        rewards_min_size=meta0.rewards_min_size,
        rewards_max_spread=meta0.rewards_max_spread,
        rewards_daily_rate=meta0.rewards_daily_rate,
        maker_fee_bps=meta0.maker_fee_bps,
        taker_fee_bps=meta0.taker_fee_bps,
        fees_enabled=meta0.fees_enabled,
        end_date_iso=meta0.end_date_iso,
        event_id=meta0.event_id,
        rebate_rate=meta0.rebate_rate,
    )

    try:
        if args.baseline_profile:
            baseline = load_named_profile(
                args.baseline_profile,
                config_dir=args.config_dir,
                overrides=base_over,
            )
        else:
            baseline = profile_from_overrides(StrategyProfile(), base_over)
        if args.candidate_profile:
            candidate = load_named_profile(
                args.candidate_profile,
                config_dir=args.config_dir,
                overrides=cand_over,
            )
        else:
            candidate = profile_from_overrides(StrategyProfile(), cand_over)
    except KeyError as exc:
        print(f"status=BAD_PROFILE {exc}", file=sys.stderr)
        return 2

    result = compare_profiles(
        journal,
        meta,
        baseline,
        candidate,
        Path(args.out_dir),
        holdout_frac=args.holdout_frac,
        use_holdout=args.use_holdout,
    )
    payload = result.as_dict()
    payload["baseline_profile"] = args.baseline_profile
    payload["candidate_profile"] = args.candidate_profile
    payload["baseline_overrides"] = base_over
    payload["candidate_overrides"] = cand_over
    print(json.dumps(payload, indent=2, sort_keys=True))

    d = result.delta
    print(
        f"status=OK window={result.window.get('mode')} "
        f"dn_quote={d.get('n_quote')} "
        f"d_spread={d.get('realized_spread_usdc')} "
        f"d_inv_peak={d.get('inventory_drift_abs_peak')} "
        f"d_cancel={d.get('n_cancel')}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
