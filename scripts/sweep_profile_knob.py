#!/usr/bin/env python3
"""Sweep one StrategyProfile knob offline and print T1-01 metric deltas.

Tier-1 eval harness for upcoming Tier-2 experiments. Does not change defaults.

Usage:
  uv run python scripts/sweep_profile_knob.py \\
      --journal fixtures/regime_jump.jsonl \\
      --baseline-profile newsom-mm \\
      --knob trend_vol_ratio --values 2,3,5,8
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import compare_profiles, load_named_profile, profile_from_overrides


def _meta(tick: float = 0.01) -> MarketMeta:
    return MarketMeta(
        condition_id="0xreplay",
        question="sweep",
        slug="sweep",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
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


def _parse_values(raw: str, knob: str) -> list[Any]:
    out: list[Any] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Infer int vs float from baseline field type via a throwaway profile.
        sample = StrategyProfile().model_dump()[knob]
        if isinstance(sample, bool):
            out.append(part.lower() in ("1", "true", "yes"))
        elif isinstance(sample, int) and not isinstance(sample, bool):
            out.append(int(float(part)))
        else:
            out.append(float(part))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--baseline-profile", default=None,
                    help="Named profile; default = StrategyProfile()")
    ap.add_argument("--knob", required=True, help="StrategyProfile field name")
    ap.add_argument("--values", required=True, help="Comma-separated candidate values")
    ap.add_argument("--tick-size", type=float, default=0.01)
    ap.add_argument("--holdout-frac", type=float, default=0.0)
    ap.add_argument("--use-holdout", action="store_true")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=NO_JOURNAL path={journal}", file=sys.stderr)
        return 2
    if args.knob not in StrategyProfile().model_dump():
        print(f"status=BAD_KNOB {args.knob}", file=sys.stderr)
        return 2

    if args.baseline_profile:
        baseline = load_named_profile(args.baseline_profile, config_dir=args.config_dir)
    else:
        baseline = StrategyProfile()
    base_val = getattr(baseline, args.knob)
    values = _parse_values(args.values, args.knob)

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="sweep_") as td:
        out_root = Path(td)
        for i, val in enumerate(values):
            cand = profile_from_overrides(baseline, {args.knob: val})
            result = compare_profiles(
                journal,
                _meta(args.tick_size),
                baseline,
                cand,
                out_root / f"v{i}",
                holdout_frac=args.holdout_frac,
                use_holdout=args.use_holdout,
            )
            rows.append({
                "knob": args.knob,
                "baseline_value": base_val,
                "candidate_value": val,
                "delta": result.delta,
                "candidate": {
                    k: result.candidate.get(k)
                    for k in ("n_quote", "n_cancel", "realized_spread_usdc",
                              "inventory_drift_abs_peak")
                },
            })

    payload = {
        "journal": str(journal),
        "baseline_profile": args.baseline_profile,
        "knob": args.knob,
        "baseline_value": base_val,
        "rows": rows,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    # Compact stderr ledger for changelog evidence
    parts = []
    for r in rows:
        dq = r["delta"].get("n_quote")
        dc = r["delta"].get("n_cancel")
        parts.append(f"{r['candidate_value']}:dn_quote={dq},dn_cancel={dc}")
    print(
        f"status=OK knob={args.knob} baseline={base_val} " + " ".join(parts),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
