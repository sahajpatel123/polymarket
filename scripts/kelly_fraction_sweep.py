#!/usr/bin/env python3
"""Sweep kelly_fraction under advanced quoting (EV + OOS + significance).

Baseline: use_as_reservation_price=true, kelly_fraction=0.25 (prior hard-coded).
Candidates: other fractions. Prints quant_edge_eval verdict per value.

Usage:
  uv run python scripts/kelly_fraction_sweep.py \\
      --journal livecfg/journal/paper.jsonl \\
      --config-dir livecfg --baseline-profile live_scaled \\
      --yes-token ... --fractions 0.125,0.25,0.5
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import load_named_profile, profile_from_overrides
from polymaker.replay.quant_edge import evaluate_quant_edge


def _meta(args: argparse.Namespace) -> MarketMeta:
    from dataclasses import replace

    meta = MarketMeta(
        condition_id=args.condition_id or "0xreplay",
        question="kelly-sweep",
        slug="kelly-sweep",
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
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--baseline-profile", default="live_scaled")
    ap.add_argument("--fractions", default="0.125,0.25,0.5")
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--n-chunks", type=int, default=4)
    ap.add_argument("--yes-token", default=None)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--report", default="logs/kelly_fraction_sweep/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    base = load_named_profile(args.baseline_profile, config_dir=args.config_dir)
    # Advanced quoting ON with quarter-Kelly as the comparison baseline
    baseline = profile_from_overrides(
        base, {"use_as_reservation_price": True, "kelly_fraction": 0.25, "bankroll_usdc": 30.0}
    )
    fracs = [float(x.strip()) for x in args.fractions.split(",") if x.strip()]
    meta = _meta(args)
    rows = []
    with tempfile.TemporaryDirectory(prefix="kelly_sweep_") as td:
        root = Path(td)
        for i, frac in enumerate(fracs):
            cand = profile_from_overrides(
                baseline, {"kelly_fraction": frac}
            )
            # Skip comparing 0.25 to itself as a "candidate improvement"
            result = evaluate_quant_edge(
                journal,
                meta,
                baseline,
                cand,
                root / f"f{i}",
                holdout_frac=args.holdout_frac,
                split="events",
                n_chunks=args.n_chunks,
            )
            d = result.as_dict()
            rows.append({
                "kelly_fraction": frac,
                "finding": d["verdict"].get("finding"),
                "full_ev_delta": d["verdict"].get("full_ev_delta"),
                "holdout_ev_delta": d["verdict"].get("holdout_ev_delta"),
                "oos_sign_match": d["verdict"].get("oos_sign_match"),
                "is_significant": d["significance"].get("is_significant"),
                "paired_p": d["significance"].get("paired_p"),
            })

    any_finding = any(r.get("finding") for r in rows if r["kelly_fraction"] != 0.25)
    out = {
        "baseline_kelly_fraction": 0.25,
        "rows": rows,
        "any_nondefault_finding": any_finding,
        "note": "finding vs quarter-Kelly advanced baseline; 0.25 row should be ~zero delta",
    }
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"status=OK journal={journal} report={path}")
    for r in rows:
        print(
            f"frac={r['kelly_fraction']} finding={r['finding']} "
            f"holdout_dn_ev={r['holdout_ev_delta']} p={r['paired_p']}"
        )
    print(f"any_nondefault_finding={any_finding}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
