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
    # mean resting USDC notional while any order open (for competition share)
    mean_resting_notional_usdc: dict[str, float] = field(default_factory=dict)
    # seconds with at least one in-band resting order
    in_band_seconds: dict[str, float] = field(default_factory=dict)
    # Quote quality counters (proves OOB_CHECK / dust filter effectiveness)
    n_dust_quotes: int = 0       # price < 0.01 (sub-cent, sub-penny)
    n_oob_quotes: int = 0        # |price - mid| > rewards_max_spread/100
    n_in_band_quotes: int = 0    # in_reward_band=True

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
            "mean_resting_notional_usdc": {
                k: round(v, 6) for k, v in self.mean_resting_notional_usdc.items()
            },
            "in_band_seconds": {k: round(v, 3) for k, v in self.in_band_seconds.items()},
            "n_dust_quotes": self.n_dust_quotes,
            "n_oob_quotes": self.n_oob_quotes,
            "n_in_band_quotes": self.n_in_band_quotes,
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
    # Resting state timeline for reward: (ts, any_in_band_resting).
    # Tracks per-order open book so cancel / empty book ends accrual
    # (a lone in-band quote then cancel must NOT earn the rest of the day).
    band_state: dict[str, list[tuple[float, bool]]] = {}
    live_in_band: dict[str, dict[str, bool]] = {}  # cid -> order_id -> in_band
    # Mean resting notional while any order is open (for share-aware PnL)
    resting_notional_samples: dict[str, list[float]] = {}

    def _push_band(cid: str, ts: float) -> None:
        live = live_in_band.get(cid) or {}
        any_band = any(live.values()) if live else False
        band_state.setdefault(cid, []).append((ts, any_band))
        # sum notional of resting in-band orders when known from last quotes
        if live:
            # notional tracked separately via quote price*size
            pass

    def _resting_notional(cid: str) -> float:
        live = live_in_band.get(cid) or {}
        return float(sum(resting_sizes.get(cid, {}).get(oid, 0.0) for oid in live))

    resting_sizes: dict[str, dict[str, float]] = {}  # cid -> order_id -> usdc notional

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
            # sample resting notional while anything is live
            if live_in_band.get(cid):
                resting_notional_samples.setdefault(cid, []).append(_resting_notional(cid))
            continue

        if ev == "quote":
            rep.n_quote += 1
            in_band = bool(e.get("in_reward_band", False))
            oid = str(e.get("order_id") or f"anon-{rep.n_quote}")
            live_in_band.setdefault(cid, {})[oid] = in_band
            try:
                px = float(e.get("price") or 0.0)
                sz = float(e.get("size") or 0.0)
                resting_sizes.setdefault(cid, {})[oid] = max(0.0, px * sz)
            except (TypeError, ValueError):
                resting_sizes.setdefault(cid, {})[oid] = 0.0
            _push_band(cid, ts)
            resting_notional_samples.setdefault(cid, []).append(_resting_notional(cid))
            # Quote quality counters (proves the band_lo filter works)
            if in_band:
                rep.n_in_band_quotes += 1
            if 0 < px < 0.01:
                # sub-cent dust (e.g. 0.001 on a 0.001-tick market) — useless
                rep.n_dust_quotes += 1
            else:
                # OOB: outside the market's reward band of the logged mid
                m_meta = meta_by_cid.get(cid, {})
                band = float(m_meta.get("rewards_max_spread") or 0.0) / 100.0
                mid = e.get("mid")
                if mid is None:
                    mid = e.get("fv_yes") or e.get("fv")
                if band > 0 and mid is not None and abs(px - float(mid)) > band + 0.01:
                    rep.n_oob_quotes += 1
            net = e.get("inventory_net")
            if net is not None:
                rep.inventory_drift_abs_peak = max(
                    rep.inventory_drift_abs_peak, abs(float(net))
                )
                rep.inventory_net_end[cid] = float(net)
            continue

        if ev == "cancel":
            rep.n_cancel += 1
            oid = str(e.get("order_id") or "")
            if oid and cid in live_in_band:
                live_in_band[cid].pop(oid, None)
                resting_sizes.get(cid, {}).pop(oid, None)
            elif cid in live_in_band and live_in_band[cid]:
                # no order_id: drop one open order (FIFO) then recompute state
                dead = next(iter(live_in_band[cid]))
                live_in_band[cid].pop(dead, None)
                resting_sizes.get(cid, {}).pop(dead, None)
            _push_band(cid, ts)  # cancel → may end in-band accrual
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

    # reward accrual: rewards_daily_rate * (seconds with ≥1 in-band resting order / 86400).
    # Marks extend the clock only while live_in_band is non-empty and any order
    # is in-band. Cancels clear orders → accrual stops (no phantom post-cancel rent).
    for cid, samples in band_state.items():
        meta = meta_by_cid.get(cid, {})
        daily = float(meta.get("rewards_daily_rate") or 0.0)
        if daily <= 0 or not samples:
            rep.reward_accrual_usdc[cid] = 0.0
            rep.in_band_seconds[cid] = 0.0
            continue
        events: list[tuple[float, bool | None]] = [(t, b) for t, b in samples]
        for t, _ in marks_by_cid.get(cid, []):
            events.append((t, None))
        events.sort(key=lambda x: x[0])
        in_band_s = 0.0
        last_t: float | None = None
        last_in_band = False
        for t, flag in events:
            if last_t is not None and last_in_band:
                in_band_s += max(0.0, t - last_t)
            if flag is not None:
                last_in_band = bool(flag)
            last_t = t
        rep.in_band_seconds[cid] = in_band_s
        rep.reward_accrual_usdc[cid] = daily * (in_band_s / 86400.0)

    for cid, samples in resting_notional_samples.items():
        if samples:
            rep.mean_resting_notional_usdc[cid] = sum(samples) / len(samples)

    return rep
