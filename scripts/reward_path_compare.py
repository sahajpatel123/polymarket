#!/usr/bin/env python3
"""Reward-path compare when AS fills are blocked (T1-153).

Under conservative fills, join+min_edge0 often shows reward_delta≈0 while
ev_per_quote can still rise from fewer quotes (denominator artifact).
This script prints reward_accrual + quote counts for baseline vs candidate.

Diagnostic only — does not change live defaults.

Usage:
  uv run python scripts/reward_path_compare.py \\
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
from typing import Any

from polymaker.metrics.analyze import analyze
from polymaker.replay import filter_rows_for_tokens, load_journal, run_replay
from polymaker.replay.compare import (
    load_named_profile,
    profile_from_overrides,
    write_sliced_journal,
)
from polymaker.replay.market_resolve import resolve_market_by_slug


def _row(label: str, mode: str, rep_metrics: Any, n_fill: int, n_cancel: int) -> dict[str, Any]:
    rew = sum(float(v) for v in (rep_metrics.reward_accrual_usdc or {}).values())
    return {
        "label": label,
        "fill_mode": mode,
        "n_quote": rep_metrics.n_quote,
        "n_fill": n_fill,
        "n_cancel": n_cancel,
        "reward_accrual_usdc": round(rew, 6),
        "ev_per_quote_usdc": round(float(rep_metrics.ev_per_quote_usdc or 0.0), 8),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--db", default="livecfg/state.db")
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument(
        "--overrides",
        default='{"join_best_bid": true, "min_edge_ticks": 0}',
    )
    ap.add_argument("--report", default="logs/reward_path_compare/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = resolve_market_by_slug(args.slug, db_path=args.db)
    rows = filter_rows_for_tokens(
        load_journal(journal),
        yes_token=meta.yes.token_id,
        no_token=meta.no.token_id,
    )
    base = load_named_profile(args.profile, config_dir=args.config_dir)
    cand = profile_from_overrides(base, json.loads(args.overrides))
    rows_out: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="rew_") as td:
        root = Path(td)
        jpath = write_sliced_journal(rows, root / "j.jsonl")
        for mode in ("conservative", "optimistic"):
            for label, prof in (("baseline", base), ("candidate", cand)):
                mpath = root / f"{label}_{mode}.jsonl"
                rr = run_replay(jpath, meta, prof, mpath, fill_mode=mode)
                rep = analyze(mpath)
                rows_out.append(_row(label, mode, rep, rr.n_fill, rr.n_cancel))

    by = {(r["label"], r["fill_mode"]): r for r in rows_out}
    summary = {}
    for mode in ("conservative", "optimistic"):
        b, c = by[("baseline", mode)], by[("candidate", mode)]
        summary[mode] = {
            "reward_delta": round(c["reward_accrual_usdc"] - b["reward_accrual_usdc"], 6),
            "ev_delta": round(c["ev_per_quote_usdc"] - b["ev_per_quote_usdc"], 8),
            "n_quote_baseline": b["n_quote"],
            "n_quote_candidate": c["n_quote"],
            "n_fill_candidate": c["n_fill"],
            "denominator_artifact": (
                c["n_fill"] == 0
                and abs(c["reward_accrual_usdc"] - b["reward_accrual_usdc"]) < 1e-9
                and c["n_quote"] < b["n_quote"]
                and c["ev_per_quote_usdc"] > b["ev_per_quote_usdc"]
            ),
        }

    report = {
        "slug": args.slug,
        "overrides": json.loads(args.overrides),
        "rows": rows_out,
        "summary": summary,
        "note": (
            "denominator_artifact=true when reward is flat, fills=0, fewer quotes, "
            "but ev_per_quote rises — not an AS finding (gated by n_fill>0)."
        ),
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    cons = summary["conservative"]
    print(
        f"status=OK cons_reward_delta={cons['reward_delta']} "
        f"cons_ev_delta={cons['ev_delta']} "
        f"denominator_artifact={cons['denominator_artifact']} report={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
