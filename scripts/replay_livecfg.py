#!/usr/bin/env python3
"""Replay livecfg paper journal(s) with token auto-detect from metrics.

Tier-1 bridge: turns collecting paper journals into offline A/B inputs without
hand-wiring token ids. Does not change strategy math.

Usage:
  uv run python scripts/replay_livecfg.py
  uv run python scripts/replay_livecfg.py --compare-profile newsom-mm
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from polymaker.config import Config, StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics.analyze import analyze
from polymaker.replay import (
    discover_condition_ids,
    infer_yes_no_tokens,
    run_replay,
)
from polymaker.replay.compare import compare_profiles, load_named_profile


def _meta(cid: str, yes: str, no: str, tick: float) -> MarketMeta:
    return MarketMeta(
        condition_id=cid,
        question=f"livecfg:{cid[:10]}",
        slug=f"livecfg-{cid[:10]}",
        tokens=(TokenMeta(yes, "Yes"), TokenMeta(no, "No")),
        tick_size=tick,
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
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--strategy-config-dir", default="config")
    ap.add_argument("--journal", default=None)
    ap.add_argument("--metrics-log", default=None,
                    help="Live metrics JSONL used to infer YES/NO tokens")
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--baseline-profile", default="live-tiny",
                    help="Profile name under --config-dir (default live-tiny)")
    ap.add_argument("--compare-profile", default=None,
                    help="Optional named profile from strategy-config-dir to A/B")
    ap.add_argument("--out-dir", default="logs/replay_livecfg")
    args = ap.parse_args()

    cfg_dir = Path(args.config_dir)
    journal = Path(args.journal) if args.journal else cfg_dir / "journal" / "paper.jsonl"
    metrics_log = (
        Path(args.metrics_log)
        if args.metrics_log
        else cfg_dir / "logs" / "metrics-paper.jsonl"
    )
    if not journal.exists():
        print(f"status=NO_JOURNAL path={journal}", file=sys.stderr)
        return 2
    if not metrics_log.exists():
        print(f"status=NO_METRICS path={metrics_log}", file=sys.stderr)
        return 2

    cfg = Config.load(cfg_dir, load_env=False)
    baseline = cfg.profiles.get(args.baseline_profile) or StrategyProfile()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cids = discover_condition_ids(metrics_log)
    markets_out = []
    for cid in cids:
        pair = infer_yes_no_tokens(metrics_log, cid)
        if pair is None:
            markets_out.append({"condition_id": cid, "error": "cannot_infer_tokens"})
            continue
        yes, no = pair
        meta = _meta(cid, yes, no, args.tick_size)
        metrics_path = out_dir / f"metrics_{cid[:10]}.jsonl"
        if metrics_path.exists():
            metrics_path.unlink()
        result = run_replay(journal, meta, baseline, metrics_path)
        rep = analyze(metrics_path).as_dict()
        entry = {
            "condition_id": cid,
            "yes_token": yes,
            "no_token": no,
            "baseline_profile": args.baseline_profile,
            "replay": {
                "events_read": result.events_read,
                "events_applied": result.events_applied,
                "recomputes": result.recomputes,
                "n_quote": result.n_quote,
                "n_cancel": result.n_cancel,
                "n_mark": result.n_mark,
            },
            "metrics": {
                k: rep.get(k)
                for k in (
                    "n_quote",
                    "n_cancel",
                    "n_fill",
                    "realized_spread_usdc",
                    "inventory_drift_abs_peak",
                    "reward_accrual_usdc",
                )
            },
        }
        if args.compare_profile:
            try:
                cand = load_named_profile(
                    args.compare_profile, config_dir=args.strategy_config_dir
                )
            except KeyError as exc:
                entry["compare_error"] = str(exc)
            else:
                with tempfile.TemporaryDirectory() as td:
                    cmp = compare_profiles(
                        journal, meta, baseline, cand, Path(td)
                    ).as_dict()
                entry["compare"] = {
                    "candidate_profile": args.compare_profile,
                    "delta": cmp.get("delta"),
                    "candidate_n_quote": (cmp.get("candidate") or {}).get("n_quote"),
                    "baseline_n_quote": (cmp.get("baseline") or {}).get("n_quote"),
                }
        markets_out.append(entry)

    payload = {
        "journal": str(journal),
        "metrics_log": str(metrics_log),
        "n_markets": len(markets_out),
        "markets": markets_out,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    total_q = sum(int((m.get("replay") or {}).get("n_quote") or 0) for m in markets_out)
    print(
        f"status=OK markets={len(markets_out)} replay_quotes={total_q} "
        f"baseline={args.baseline_profile} compare={args.compare_profile}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
