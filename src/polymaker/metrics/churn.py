"""Quote lifetime and requote-interval churn stats from metrics JSONL.

Tier-1 evidence for T2-05 (update frequency). Uses the same resting-quote
lifetime construction as shadow AS (replace/cancel closes a life), then
reports lifetime and inter-quote interval percentiles — no strategy edits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from polymaker.metrics.analyze import load_events


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


@dataclass
class _Resting:
    order_id: str
    key: tuple[str, str, str]
    ts0: float


@dataclass
class ChurnReport:
    path: str
    n_lifetimes: int = 0
    lifetime_p50_s: float = 0.0
    lifetime_p95_s: float = 0.0
    lifetime_mean_s: float = 0.0
    n_intervals: int = 0
    requote_interval_p50_s: float = 0.0
    requote_interval_p95_s: float = 0.0
    requote_interval_mean_s: float = 0.0
    by_market: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "n_lifetimes": self.n_lifetimes,
            "lifetime_p50_s": round(self.lifetime_p50_s, 6),
            "lifetime_p95_s": round(self.lifetime_p95_s, 6),
            "lifetime_mean_s": round(self.lifetime_mean_s, 6),
            "n_intervals": self.n_intervals,
            "requote_interval_p50_s": round(self.requote_interval_p50_s, 6),
            "requote_interval_p95_s": round(self.requote_interval_p95_s, 6),
            "requote_interval_mean_s": round(self.requote_interval_mean_s, 6),
            "by_market": self.by_market,
        }


def analyze_quote_churn(path: Path) -> ChurnReport:
    events, _, _ = load_events(path)
    rep = ChurnReport(path=str(path))

    active: dict[tuple[str, str, str], _Resting] = {}
    by_oid: dict[str, _Resting] = {}
    lifetimes: list[float] = []
    lifetimes_by_cid: dict[str, list[float]] = {}
    quote_ts_by_cid: dict[str, list[float]] = {}

    def close(r: _Resting, ts_end: float) -> None:
        life = max(0.0, ts_end - r.ts0)
        if life > 0:
            lifetimes.append(life)
            cid = r.key[0]
            lifetimes_by_cid.setdefault(cid, []).append(life)
        cur = active.get(r.key)
        if cur is not None and cur.order_id == r.order_id:
            active.pop(r.key, None)
        by_oid.pop(r.order_id, None)

    for e in events:
        ev = str(e.get("event"))
        ts = float(e.get("ts") or 0.0)
        cid = str(e.get("condition_id") or "")
        if ev == "quote":
            oid = str(e.get("order_id") or "")
            token = str(e.get("token_id") or "")
            side = str(e.get("side") or "")
            if not oid or not cid or side not in ("BUY", "SELL"):
                continue
            key = (cid, token, side)
            prev = active.get(key)
            if prev is not None and prev.order_id != oid:
                close(prev, ts)
            resting = _Resting(order_id=oid, key=key, ts0=ts)
            active[key] = resting
            by_oid[oid] = resting
            quote_ts_by_cid.setdefault(cid, []).append(ts)
            continue
        if ev == "cancel":
            oid = str(e.get("order_id") or "")
            r = by_oid.get(oid)
            if r is not None:
                close(r, ts)

    if events:
        t_end = float(events[-1].get("ts") or 0.0)
        for r in list(active.values()):
            close(r, t_end)

    intervals: list[float] = []
    intervals_by_cid: dict[str, list[float]] = {}
    for cid, stamps in quote_ts_by_cid.items():
        stamps = sorted(stamps)
        for a, b in zip(stamps, stamps[1:]):
            dt = b - a
            if dt > 0:
                intervals.append(dt)
                intervals_by_cid.setdefault(cid, []).append(dt)

    def summarize(vals: list[float]) -> dict[str, float]:
        s = sorted(vals)
        mean = sum(s) / len(s) if s else 0.0
        return {
            "n": len(s),
            "p50_s": round(_percentile(s, 50), 6),
            "p95_s": round(_percentile(s, 95), 6),
            "mean_s": round(mean, 6),
        }

    life_s = sorted(lifetimes)
    int_s = sorted(intervals)
    rep.n_lifetimes = len(life_s)
    rep.lifetime_p50_s = _percentile(life_s, 50)
    rep.lifetime_p95_s = _percentile(life_s, 95)
    rep.lifetime_mean_s = (sum(life_s) / len(life_s)) if life_s else 0.0
    rep.n_intervals = len(int_s)
    rep.requote_interval_p50_s = _percentile(int_s, 50)
    rep.requote_interval_p95_s = _percentile(int_s, 95)
    rep.requote_interval_mean_s = (sum(int_s) / len(int_s)) if int_s else 0.0

    markets = sorted(set(lifetimes_by_cid) | set(intervals_by_cid))
    for cid in markets:
        rep.by_market[cid] = {
            "lifetime": summarize(lifetimes_by_cid.get(cid, [])),
            "requote_interval": summarize(intervals_by_cid.get(cid, [])),
        }
    return rep
