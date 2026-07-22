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


from polymaker.metrics.log_discovery import (
    DEFAULT_METRICS_CANDIDATES,
    DEFAULT_PAPER_CANDIDATES,
    pick_richest_log,
)


def _load_script_build_scorecard():
    import importlib.util

    path = Path(__file__).resolve().parent / "reward_scorecard.py"
    spec = importlib.util.spec_from_file_location("reward_scorecard", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_scorecard


def _reward_decomposition(metrics: Path) -> dict[str, dict[str, float]]:
    """Per-market daily_rate, in-band hours, and accrual from metrics JSONL."""
    meta: dict[str, dict] = {}
    band: dict[str, list[tuple[float, bool]]] = {}
    with metrics.open() as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = str(obj.get("condition_id") or "")
            if not cid:
                continue
            if obj.get("event") == "market_meta":
                meta[cid] = obj
            elif obj.get("event") == "quote":
                try:
                    ts = float(obj.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                band.setdefault(cid, []).append((ts, bool(obj.get("in_reward_band", False))))
    out: dict[str, dict[str, float]] = {}
    for cid, samples in band.items():
        samples = sorted(samples)
        daily = float((meta.get(cid) or {}).get("rewards_daily_rate") or 0.0)
        if len(samples) < 2:
            out[cid] = {
                "rewards_daily_rate": daily,
                "in_band_hours": 0.0,
                "quote_span_hours": 0.0,
                "in_band_frac": 0.0,
            }
            continue
        in_s = 0.0
        for (t0, b0), (t1, _) in zip(samples, samples[1:]):
            if b0:
                in_s += max(0.0, t1 - t0)
        span = max(0.0, samples[-1][0] - samples[0][0])
        out[cid] = {
            "rewards_daily_rate": daily,
            "in_band_hours": round(in_s / 3600.0, 6),
            "quote_span_hours": round(span / 3600.0, 6),
            "in_band_frac": round(in_s / span, 6) if span > 0 else 0.0,
        }
    return out


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
            "scanner_spread": detail.get("spread"),
            "scanner_extremity": detail.get("extremity"),
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
    decomp = _reward_decomposition(metrics)
    rows = []
    for m in card.get("markets") or []:
        cid = m["condition_id"]
        cat = catalog.get(cid) or {}
        d = decomp.get(cid) or {}
        rows.append({
            "condition_id": cid,
            "slug": cat.get("slug"),
            "scanner_score": cat.get("scanner_score"),
            "reward_density": cat.get("reward_density"),
            "rebate_potential": cat.get("rebate_potential"),
            "scanner_spread": cat.get("scanner_spread"),
            "scanner_extremity": cat.get("scanner_extremity"),
            "rewards_daily_rate": d.get("rewards_daily_rate"),
            "in_band_hours": d.get("in_band_hours"),
            "in_band_frac": d.get("in_band_frac"),
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
    by_oracle = sorted(
        [r for r in rows if r.get("rewards_daily_rate") is not None],
        key=lambda r: float(r["rewards_daily_rate"]),
        reverse=True,
    )
    for i, r in enumerate(by_scan, 1):
        r["scanner_rank"] = i
    for i, r in enumerate(by_real, 1):
        r["realized_rank"] = i
    for i, r in enumerate(by_oracle, 1):
        r["liquidity_oracle_rank"] = i
    # align for correlation
    aligned = [r for r in rows if r.get("scanner_score") is not None and r.get("realized_reward_per_hour") is not None]
    rho = spearman_rank_corr(
        [float(r["scanner_score"]) for r in aligned],
        [float(r["realized_reward_per_hour"]) for r in aligned],
    )
    rho_oracle = spearman_rank_corr(
        [float(r["scanner_score"]) for r in aligned if r.get("rewards_daily_rate") is not None],
        [float(r["rewards_daily_rate"]) for r in aligned if r.get("rewards_daily_rate") is not None],
    )
    disagree = [
        {
            "condition_id": r["condition_id"],
            "slug": r.get("slug"),
            "scanner_rank": r.get("scanner_rank"),
            "realized_rank": r.get("realized_rank"),
            "liquidity_oracle_rank": r.get("liquidity_oracle_rank"),
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
        "spearman_scanner_vs_liquidity_oracle": rho_oracle,
        "rank_disagreements": disagree,
        "markets": sorted(aligned, key=lambda r: float(r.get("realized_reward_per_hour") or 0), reverse=True),
        "note": (
            "liquidity_oracle_rank sorts by rewards_daily_rate only; when paper "
            "fills=0 and in_band_frac≈1, realized ranks should match this oracle."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--paper-log", default=None)
    args = ap.parse_args()
    db = Path(args.db) if args.db else next(
        (p for p in (Path("livecfg/state.db"), Path("state.db")) if p.exists()),
        Path("livecfg/state.db"),
    )
    metrics = Path(args.metrics) if args.metrics else pick_richest_log(
        DEFAULT_METRICS_CANDIDATES
    )
    paper = Path(args.paper_log) if args.paper_log else pick_richest_log(
        DEFAULT_PAPER_CANDIDATES
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
        f"spearman_vs_oracle={rep['spearman_scanner_vs_liquidity_oracle']} "
        f"disagreements={len(rep['rank_disagreements'])}",
        file=sys.stderr,
    )
    for m in rep.get("markets") or []:
        print(
            f"market slug={m.get('slug')} scanner_rank={m.get('scanner_rank')} "
            f"realized_rank={m.get('realized_rank')} daily_rate={m.get('rewards_daily_rate')} "
            f"in_band_frac={m.get('in_band_frac')} rebate_pot={m.get('rebate_potential')} "
            f"reward_density={m.get('reward_density')} extremity={m.get('scanner_extremity')} "
            f"rph={m.get('realized_reward_per_hour')}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
