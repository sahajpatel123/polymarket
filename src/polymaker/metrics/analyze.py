"""Pure metrics computation over a MetricsLogger JSONL file.

No I/O beyond reading the provided path. Used by scripts/paper_metrics.py and
unit tests — Rule 0 evidence must come from this script's printed output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKOUT_HORIZONS_S = (30.0, 120.0, 300.0)


@dataclass
class MetricsReport:
    path: str
    n_lines: int = 0
    n_bad: int = 0
    n_quote: int = 0
    n_cancel: int = 0
    n_fill: int = 0
    n_mark: int = 0
    markets: set[str] = field(default_factory=set)
    # realized maker edge estimate: for each fill, edge vs contemporaneous mid
    realized_spread_usdc: float = 0.0
    # adverse selection: mean signed markout (positive = good for us)
    markout: dict[str, float] = field(default_factory=dict)
    markout_n: dict[str, int] = field(default_factory=dict)
    # inventory
    inventory_drift_abs_peak: float = 0.0
    inventory_net_end: dict[str, float] = field(default_factory=dict)
    # reward / rebate accrual estimates from logged meta + quote time-in-band
    reward_accrual_usdc: dict[str, float] = field(default_factory=dict)
    rebate_pool_daily_usdc: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "n_lines": self.n_lines,
            "n_bad": self.n_bad,
            "n_quote": self.n_quote,
            "n_cancel": self.n_cancel,
            "n_fill": self.n_fill,
            "n_mark": self.n_mark,
            "markets": sorted(self.markets),
            "realized_spread_usdc": round(self.realized_spread_usdc, 6),
            "markout_mean": {k: round(v, 6) for k, v in self.markout.items()},
            "markout_n": dict(self.markout_n),
            "inventory_drift_abs_peak": round(self.inventory_drift_abs_peak, 6),
            "inventory_net_end": {k: round(v, 6) for k, v in self.inventory_net_end.items()},
            "reward_accrual_usdc": {k: round(v, 6) for k, v in self.reward_accrual_usdc.items()},
            "rebate_pool_daily_usdc": {
                k: round(v, 6) for k, v in self.rebate_pool_daily_usdc.items()
            },
        }


def load_events(path: Path) -> tuple[list[dict[str, Any]], int, int]:
    events: list[dict[str, Any]] = []
    n_lines = 0
    n_bad = 0
    if not path.exists():
        return events, 0, 0
    with path.open() as fh:
        for line in fh:
            n_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                continue
            if isinstance(obj, dict) and "event" in obj:
                events.append(obj)
            else:
                n_bad += 1
    return events, n_lines, n_bad


def analyze(path: Path) -> MetricsReport:
    events, n_lines, n_bad = load_events(path)
    rep = MetricsReport(path=str(path), n_lines=n_lines, n_bad=n_bad)

    marks_by_cid: dict[str, list[tuple[float, float]]] = {}
    meta_by_cid: dict[str, dict[str, Any]] = {}
    # quote intervals in-band for reward estimate: list of (ts, in_band)
    quote_band: dict[str, list[tuple[float, bool]]] = {}

    for e in events:
        ev = str(e.get("event"))
        cid = str(e.get("condition_id") or "")
        if cid:
            rep.markets.add(cid)
        ts = float(e.get("ts") or 0.0)

        if ev == "market_meta":
            meta_by_cid[cid] = e
            if e.get("rebate_potential_daily") is not None:
                rep.rebate_pool_daily_usdc[cid] = float(e["rebate_potential_daily"])
            continue

        if ev == "mark":
            rep.n_mark += 1
            fv = e.get("fv")
            if fv is not None:
                marks_by_cid.setdefault(cid, []).append((ts, float(fv)))
            net = e.get("inventory_net")
            if net is not None:
                rep.inventory_drift_abs_peak = max(
                    rep.inventory_drift_abs_peak, abs(float(net))
                )
                rep.inventory_net_end[cid] = float(net)
            continue

        if ev == "quote":
            rep.n_quote += 1
            in_band = bool(e.get("in_reward_band", False))
            quote_band.setdefault(cid, []).append((ts, in_band))
            net = e.get("inventory_net")
            if net is not None:
                rep.inventory_drift_abs_peak = max(
                    rep.inventory_drift_abs_peak, abs(float(net))
                )
                rep.inventory_net_end[cid] = float(net)
            continue

        if ev == "cancel":
            rep.n_cancel += 1
            net = e.get("inventory_net")
            if net is not None:
                rep.inventory_net_end[cid] = float(net)
            continue

        if ev == "fill":
            rep.n_fill += 1
            price = float(e.get("price") or 0.0)
            size = float(e.get("size") or 0.0)
            side = str(e.get("side") or "")
            mid = e.get("mid")
            if mid is None:
                mid = e.get("fv")
            if mid is not None and size > 0:
                m = float(mid)
                # maker BUY below mid earns (mid - price); SELL above mid earns (price - mid)
                if side == "BUY":
                    rep.realized_spread_usdc += (m - price) * size
                elif side == "SELL":
                    rep.realized_spread_usdc += (price - m) * size
            net = e.get("inventory_net")
            if net is not None:
                rep.inventory_drift_abs_peak = max(
                    rep.inventory_drift_abs_peak, abs(float(net))
                )
                rep.inventory_net_end[cid] = float(net)

            # schedule markouts vs later marks
            fv0 = float(mid) if mid is not None else None
            if fv0 is not None and cid in marks_by_cid:
                pass  # evaluated in second pass below
            e["_fv0"] = fv0
            continue

    # second pass: adverse-selection markouts using marks after each fill
    for horizon in MARKOUT_HORIZONS_S:
        key = f"{int(horizon)}s"
        vals: list[float] = []
        for e in events:
            if e.get("event") != "fill":
                continue
            cid = str(e.get("condition_id") or "")
            fv0 = e.get("_fv0")
            if fv0 is None:
                continue
            ts = float(e.get("ts") or 0.0)
            side = str(e.get("side") or "")
            target = ts + horizon
            # first mark at or after target
            fv1 = None
            for mts, mfv in marks_by_cid.get(cid, []):
                if mts >= target:
                    fv1 = mfv
                    break
            if fv1 is None:
                continue
            move = fv1 - float(fv0)
            # BUY: rise is good; SELL: fall is good
            signed = move if side == "BUY" else -move
            vals.append(signed)
        if vals:
            rep.markout[key] = sum(vals) / len(vals)
            rep.markout_n[key] = len(vals)
        else:
            rep.markout[key] = 0.0
            rep.markout_n[key] = 0

    # reward accrual: if we had any in-band quote, accrue rewards_daily_rate *
    # (time span of in-band quotes / 86400). Crude share=1 placeholder until
    # competition is logged — still computed purely from the metrics log.
    for cid, samples in quote_band.items():
        meta = meta_by_cid.get(cid, {})
        daily = float(meta.get("rewards_daily_rate") or 0.0)
        if daily <= 0 or len(samples) < 2:
            rep.reward_accrual_usdc[cid] = 0.0
            continue
        samples = sorted(samples)
        in_band_s = 0.0
        for (t0, b0), (t1, _) in zip(samples, samples[1:], strict=False):
            if b0:
                in_band_s += max(0.0, t1 - t0)
        rep.reward_accrual_usdc[cid] = daily * (in_band_s / 86400.0)

    return rep
