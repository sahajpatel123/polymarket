#!/usr/bin/env python3
"""Compare catalog scanner ranks vs realized paper reward/hour (T2-01 evidence).

Reads SQLite catalog scores and live metrics reward accrual. Pure reporting —
does not change scoring math.

Usage:
  uv run python scripts/rank_vs_realized.py
  uv run python scripts/rank_vs_realized.py --db livecfg/state.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _load_script_build_scorecard():
    import importlib.util

    path = Path(__file__).resolve().parent / "reward_scorecard.py"
    spec = importlib.util.spec_from_file_location("reward_scorecard", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_scorecard


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def load_catalog_scores(db: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT condition_id, slug, score, score_json FROM markets"
        ).fetchall()
    finally:
        con.close()
    for cid, slug, score, score_json in rows:
        detail = {}
        if score_json:
            try:
                detail = json.loads(score_json)
            except json.JSONDecodeError:
                detail = {}
        out[str(cid)] = {
            "condition_id": str(cid),
            "slug": slug,
            "scanner_score": float(score or 0.0),
            "reward_density": detail.get("reward_density"),
            "rebate_potential": detail.get("rebate_potential"),
        }
    return out


def spearman_rank_corr(xs: list[float], ys: list[float]) -> float | None:
    """Spearman ρ on ranks; None if <2 points or ties-only degenerate."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = sum((a - mx) ** 2 for a in rx) ** 0.5
    deny = sum((b - my) ** 2 for b in ry) ** 0.5
    if denx == 0 or deny == 0:
        return None
    return round(num / (denx * deny), 6)


def build_report(
    *,
    db: Path,
    metrics: Path,
    paper_log: Path | None,
) -> dict[str, Any]:
    build_scorecard = _load_script_build_scorecard()
    card = build_scorecard(metrics, paper_log)
    catalog = load_catalog_scores(db)
    rows = []
    for m in card.get("markets") or []:
        cid = m["condition_id"]
        cat = catalog.get(cid) or {}
        rows.append({
            "condition_id": cid,
            "slug": cat.get("slug"),
            "scanner_score": cat.get("scanner_score"),
            "reward_density": cat.get("reward_density"),
            "realized_reward_per_hour": m.get("reward_per_hour_usdc"),
            "realized_reward_accrual_usdc": m.get("reward_accrual_usdc"),
            "n_quote": m.get("n_quote"),
            "trending_frac": (m.get("regime") or {}).get("trending_frac"),
        })
    # ranks: 1 = best
    by_scan = sorted(
        [r for r in rows if r.get("scanner_score") is not None],
        key=lambda r: float(r["scanner_score"]),
        reverse=True,
    )
    by_real = sorted(
        [r for r in rows if r.get("realized_reward_per_hour") is not None],
        key=lambda r: float(r["realized_reward_per_hour"]),
        reverse=True,
    )
    for i, r in enumerate(by_scan, 1):
        r["scanner_rank"] = i
    for i, r in enumerate(by_real, 1):
        r["realized_rank"] = i
    # align for correlation
    aligned = [r for r in rows if r.get("scanner_score") is not None and r.get("realized_reward_per_hour") is not None]
    rho = spearman_rank_corr(
        [float(r["scanner_score"]) for r in aligned],
        [float(r["realized_reward_per_hour"]) for r in aligned],
    )
    disagree = [
        {
            "condition_id": r["condition_id"],
            "slug": r.get("slug"),
            "scanner_rank": r.get("scanner_rank"),
            "realized_rank": r.get("realized_rank"),
        }
        for r in aligned
        if r.get("scanner_rank") != r.get("realized_rank")
    ]
    return {
        "db": str(db),
        "metrics": str(metrics),
        "runtime_hours": card.get("runtime_hours"),
        "n_markets": len(aligned),
        "spearman_scanner_vs_realized": rho,
        "rank_disagreements": disagree,
        "markets": sorted(aligned, key=lambda r: float(r.get("realized_reward_per_hour") or 0), reverse=True),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--paper-log", default=None)
    args = ap.parse_args()
    db = Path(args.db) if args.db else _first_existing(
        Path("livecfg/state.db"), Path("state.db")
    )
    metrics = Path(args.metrics) if args.metrics else _first_existing(
        Path("livecfg/logs/metrics-paper.jsonl"),
        Path("logs/metrics-paper.jsonl"),
    )
    paper = Path(args.paper_log) if args.paper_log else _first_existing(
        Path("livecfg/logs/paper.jsonl"),
        Path("logs/paper.jsonl"),
    )
    if db is None or not db.exists():
        print("status=NO_DB", file=sys.stderr)
        return 2
    if metrics is None or not metrics.exists():
        print("status=NO_METRICS", file=sys.stderr)
        return 2
    rep = build_report(db=db, metrics=metrics, paper_log=paper)
    print(json.dumps(rep, indent=2, sort_keys=True))
    print(
        f"status=OK n={rep['n_markets']} spearman={rep['spearman_scanner_vs_realized']} "
        f"disagreements={len(rep['rank_disagreements'])}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
