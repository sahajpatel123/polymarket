#!/usr/bin/env python3
"""Sweep c_kyle (Kyle λ → half-spread) for quote EV (OOS + significance).

Baseline: named profile with c_kyle=0 (default).
Candidates: alternate c_kyle > 0. Documents whether Kyle quote wiring
improves EV — Spearman skill alone does not promote (T1-154).

Usage:
  uv run python scripts/kyle_ev_sweep.py \\
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
    ap.add_argument("--values", default="0.5,1.0,2.0")
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--n-chunks", type=int, default=8)
    ap.add_argument(
        "--fill-mode",
        choices=("conservative", "base", "optimistic"),
        default="conservative",
    )
    ap.add_argument("--report", default="logs/kyle_ev_sweep/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = resolve_market_by_slug(args.slug, db_path=args.db)
    baseline = load_named_profile(args.baseline_profile, config_dir=args.config_dir)
    # Ensure baseline is c_kyle=0 for a clean control.
    baseline = profile_from_overrides(baseline, {"c_kyle": 0.0})
    values = [float(x.strip()) for x in args.values.split(",") if x.strip()]
    rows: list[dict] = []

    for c in values:
        with tempfile.TemporaryDirectory(prefix=f"kyle_ev_{c}_") as tmp:
            candidate = profile_from_overrides(baseline, {"c_kyle": c})
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
                "c_kyle": c,
                "finding": bool(d["verdict"].get("finding")),
                "ev_signal": bool(d["verdict"].get("ev_signal")),
                "promotion_eligible": bool(d["verdict"].get("promotion_eligible")),
                "n_fill_baseline": d["verdict"].get("n_fill_baseline"),
                "n_fill_candidate": d["verdict"].get("n_fill_candidate"),
                "holdout_ev_delta": d["verdict"].get("holdout_ev_delta"),
                "full_ev_delta": d["verdict"].get("full_ev_delta"),
                "is_significant": d["verdict"].get("is_significant"),
                "oos_sign_match": d["verdict"].get("oos_sign_match"),
                "paired_p": d["significance"].get("paired_p"),
                "reward_accrual_delta": d["verdict"].get("reward_accrual_delta"),
            }
            rows.append(row)
            print(
                f"c_kyle={c} finding={row['finding']} ev_signal={row['ev_signal']} "
                f"holdout_dn_ev={row['holdout_ev_delta']} p={row['paired_p']} "
                f"n_fill={row['n_fill_baseline']}->{row['n_fill_candidate']}"
            )

    report = {
        "slug": args.slug,
        "baseline_c_kyle": 0.0,
        "fill_mode": args.fill_mode,
        "values": values,
        "any_finding": any(r["finding"] for r in rows),
        "any_ev_signal": any(r["ev_signal"] for r in rows),
        "rows": rows,
        "note": (
            "c_kyle default stays 0 unless multi-market finding=true with fills; "
            "Spearman vs |Δmid| alone does not promote quote wiring"
        ),
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"status=OK any_finding={report['any_finding']} report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
