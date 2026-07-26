#!/usr/bin/env python3
"""Sweep layer_step_ticks (depth spacing) for quote EV (OOS + significance).

Baseline: named profile layer_step_ticks (live_scaled ≈ 2).
Candidates: alternate spacings. Tier-1 evidence only — does not change
defaults (T1-161).

Usage:
  uv run python scripts/layer_step_ev_sweep.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --slug will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \\
      --db livecfg/state.db --config-dir livecfg
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from polymaker.replay.compare import load_named_profile, profile_from_overrides
from polymaker.replay.market_resolve import resolve_market_by_slug
from polymaker.replay.quant_edge import evaluate_quant_edge


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--db", default="livecfg/state.db")
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--baseline-profile", default="live_scaled")
    ap.add_argument("--values", default="1,2,3,4")
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--n-chunks", type=int, default=8)
    ap.add_argument(
        "--fill-mode",
        choices=("conservative", "base", "optimistic"),
        default="conservative",
    )
    ap.add_argument("--report", default="logs/layer_step_ev_sweep/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = resolve_market_by_slug(args.slug, db_path=args.db)
    baseline = load_named_profile(args.baseline_profile, config_dir=args.config_dir)
    base_v = int(baseline.layer_step_ticks)
    values = [int(float(x.strip())) for x in args.values.split(",") if x.strip()]
    rows: list[dict] = []

    for v in values:
        with tempfile.TemporaryDirectory(prefix=f"layer_step_ev_{v}_") as tmp:
            candidate = profile_from_overrides(baseline, {"layer_step_ticks": v})
            ev = evaluate_quant_edge(
                journal,
                meta,
                baseline=baseline,
                candidate=candidate,
                out_dir=Path(tmp),
                holdout_frac=args.holdout_frac,
                split="events",
                n_chunks=args.n_chunks,
                fill_mode=args.fill_mode,
            )
            d = ev.as_dict()
            row = {
                "layer_step_ticks": v,
                "is_baseline": v == base_v,
                "finding": bool(d["verdict"].get("finding")),
                "ev_signal": bool(d["verdict"].get("ev_signal")),
                "promotion_eligible": bool(d["verdict"].get("promotion_eligible")),
                "n_fill_baseline": d["verdict"].get("n_fill_baseline"),
                "n_fill_candidate": d["verdict"].get("n_fill_candidate"),
                "n_quote_baseline": d["full"]["baseline"].get("n_quote"),
                "n_quote_candidate": d["full"]["candidate"].get("n_quote"),
                "holdout_ev_delta": d["verdict"].get("holdout_ev_delta"),
                "full_ev_delta": d["verdict"].get("full_ev_delta"),
                "is_significant": d["verdict"].get("is_significant"),
                "oos_sign_match": d["verdict"].get("oos_sign_match"),
                "paired_p": d["significance"].get("paired_p"),
                "reward_accrual_delta": d["verdict"].get("reward_accrual_delta"),
            }
            rows.append(row)
            print(
                f"layer_step={v} finding={row['finding']} ev_signal={row['ev_signal']} "
                f"n_quote={row['n_quote_baseline']}->{row['n_quote_candidate']} "
                f"holdout_dn_ev={row['holdout_ev_delta']} p={row['paired_p']} "
                f"baseline={row['is_baseline']}"
            )

    report = {
        "slug": args.slug,
        "baseline_layer_step_ticks": base_v,
        "fill_mode": args.fill_mode,
        "values": values,
        "any_nondefault_finding": any(
            r["finding"] and not r["is_baseline"] for r in rows
        ),
        "rows": rows,
        "note": (
            "changing default layer_step_ticks requires non-baseline finding=true "
            "with fills on multi-market tape"
        ),
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"status=OK any_nondefault_finding={report['any_nondefault_finding']} "
        f"report={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
