#!/usr/bin/env python3
"""Per-market reward/churn scorecard from live paper metrics + requotes.

Tier-1 evidence surface for T2-01 (reward-ranking). Read-only.

Usage:
  uv run python scripts/reward_scorecard.py
  uv run python scripts/reward_scorecard.py --metrics livecfg/logs/metrics-paper.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from polymaker.metrics.analyze import analyze


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def _runtime_hours(metrics_path: Path) -> float:
    times: list[float] = []
    with metrics_path.open() as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = obj.get("ts")
            if isinstance(ts, (int, float)):
                times.append(float(ts))
    if len(times) < 2:
        return 0.0
    return max(0.0, (max(times) - min(times)) / 3600.0)


def _regime_by_market(paper_log: Path | None) -> dict[str, dict[str, Any]]:
    if paper_log is None or not paper_log.exists():
        return {}
    regimes: dict[str, Counter] = defaultdict(Counter)
    cancel: dict[str, int] = defaultdict(int)
    place: dict[str, int] = defaultdict(int)
    with paper_log.open() as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(obj.get("event") or "") != "requote":
                continue
            cid = str(obj.get("condition_id") or obj.get("cid") or "unknown")
            regimes[cid][str(obj.get("regime") or "UNKNOWN")] += 1
            cancel[cid] += int(obj.get("cancel") or 0)
            place[cid] += int(obj.get("place") or 0)
    out: dict[str, dict[str, Any]] = {}
    for cid, ctr in regimes.items():
        n = sum(ctr.values())
        out[cid] = {
            "regimes": dict(ctr),
            "trending_frac": round(ctr.get("TRENDING", 0) / n, 6) if n else 0.0,
            "cancel_sum": cancel[cid],
            "place_sum": place[cid],
            "cancel_per_place": round(cancel[cid] / place[cid], 6) if place[cid] else None,
        }
    return out


def build_scorecard(metrics_path: Path, paper_log: Path | None) -> dict[str, Any]:
    rep = analyze(metrics_path)
    hours = _runtime_hours(metrics_path)
    regime = _regime_by_market(paper_log)

    # Per-market quote/cancel from raw metrics
    q_n: dict[str, int] = defaultdict(int)
    c_n: dict[str, int] = defaultdict(int)
    with metrics_path.open() as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = str(obj.get("condition_id") or "")
            if not cid:
                continue
            if obj.get("event") == "quote":
                q_n[cid] += 1
            elif obj.get("event") == "cancel":
                c_n[cid] += 1

    markets: list[dict[str, Any]] = []
    reward = rep.reward_accrual_usdc or {}
    rebate = rep.rebate_pool_daily_usdc or {}
    for cid in sorted(set(reward) | set(rebate) | set(q_n) | set(regime)):
        r = float(reward.get(cid, 0.0) or 0.0)
        markets.append({
            "condition_id": cid,
            "reward_accrual_usdc": round(r, 6),
            "reward_per_hour_usdc": round(r / hours, 6) if hours > 0 else None,
            "rebate_pool_daily_usdc": float(rebate.get(cid, 0.0) or 0.0),
            "n_quote": q_n.get(cid, 0),
            "n_cancel": c_n.get(cid, 0),
            "regime": regime.get(cid),
        })
    markets.sort(key=lambda m: float(m.get("reward_per_hour_usdc") or 0.0), reverse=True)
    return {
        "metrics_path": str(metrics_path),
        "paper_log": str(paper_log) if paper_log else None,
        "runtime_hours": round(hours, 4),
        "n_quote_total": rep.n_quote,
        "n_cancel_total": rep.n_cancel,
        "n_fill_total": rep.n_fill,
        "reward_accrual_sum": round(sum(float(v) for v in reward.values()), 6),
        "markets": markets,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metrics", default=None)
    ap.add_argument("--paper-log", default=None)
    args = ap.parse_args()
    metrics = Path(args.metrics) if args.metrics else _first_existing(
        Path("livecfg/logs/metrics-paper.jsonl"),
        Path("logs/metrics-paper.jsonl"),
    )
    paper = Path(args.paper_log) if args.paper_log else _first_existing(
        Path("livecfg/logs/paper.jsonl"),
        Path("logs/paper.jsonl"),
    )
    if metrics is None or not metrics.exists():
        print("status=NO_METRICS", file=sys.stderr)
        return 2
    card = build_scorecard(metrics, paper)
    print(json.dumps(card, indent=2, sort_keys=True))
    top = card["markets"][0]["condition_id"][:10] if card["markets"] else "-"
    top_rph = card["markets"][0].get("reward_per_hour_usdc") if card["markets"] else None
    print(
        f"status=OK markets={len(card['markets'])} runtime_h={card['runtime_hours']} "
        f"reward_sum={card['reward_accrual_sum']} top={top} reward_per_hour={top_rph}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
