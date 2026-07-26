"""Flow_z directional calibration (proper scoring vs climatology).

Maps EWMA signed-flow z-score to P(mid up) and scores on OOS holdout with
Brier vs tune climatology — same evidence standard as OFI/VPIN signal_calibration.

Eval infra only — does not change quote math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_last_trade, parse_price_changes
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.replay.signal_calibration import _score_binary
from polymaker.strategy.estimators import FlowEstimator


def flow_z_to_prob_up(z: float, *, scale: float = 1.0) -> float:
    """Map flow z (~[-1,1]+) to P(up). 0 → 0.5."""
    x = max(-1.0, min(1.0, float(z) * float(scale)))
    return 0.5 + 0.5 * x


def _mid(book: OrderBook) -> float | None:
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None or bb.price >= ba.price:
        return None
    return 0.5 * (bb.price + ba.price)


@dataclass(frozen=True)
class FlowSample:
    ts: float
    mid: float
    prob: float


def _collect(
    rows: list[dict[str, Any]],
    yes_token: str,
    *,
    sample_every: int,
    flow_halflife_s: float,
) -> list[FlowSample]:
    book = OrderBook()
    flow = FlowEstimator(flow_halflife_s)
    out: list[FlowSample] = []
    n = 0
    for row in rows:
        kind = row.get("kind")
        data = row.get("data")
        ts = float(row.get("ts") or 0.0)
        if not isinstance(data, dict):
            continue
        if kind == "last_trade_price":
            tp = parse_last_trade(data)
            if tp is None or tp.asset_id != yes_token:
                continue
            flow.update(tp.aggressor, tp.size, float(tp.ts or ts))
            continue
        if kind == "book":
            upd = parse_book(data)
            if upd is None or upd.asset_id != yes_token:
                continue
            if upd.tick_size:
                book.set_tick_size(upd.tick_size)
            book.apply_snapshot(upd.bids, upd.asks, upd.ts or ts, upd.book_hash)
        elif kind == "price_change":
            touched = False
            for ch in parse_price_changes(data):
                if ch.asset_id != yes_token:
                    continue
                book.apply_delta(ch.side, ch.price, ch.size, ch.ts or ts)
                touched = True
            if not touched:
                continue
        else:
            continue
        mid = _mid(book)
        if mid is None:
            continue
        flow.decay_to(ts)
        n += 1
        if sample_every > 1 and (n % sample_every) != 0:
            continue
        out.append(FlowSample(ts=ts, mid=mid, prob=flow_z_to_prob_up(flow.z)))
    return out


def _pair(samples: list[FlowSample], horizon_s: float) -> tuple[list[float], list[float]]:
    probs: list[float] = []
    ys: list[float] = []
    j = 0
    for s in samples:
        target = s.ts + horizon_s
        while j < len(samples) and samples[j].ts < target:
            j += 1
        if j >= len(samples):
            break
        probs.append(s.prob)
        ys.append(1.0 if samples[j].mid > s.mid else 0.0)
    return probs, ys


@dataclass(frozen=True)
class FlowCalibrationReport:
    n: int
    horizon_s: float
    flow: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "horizon_s": self.horizon_s,
            "flow": self.flow,
            "verdict": self.verdict,
        }


def calibrate_flow(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    horizon_s: float = 30.0,
    sample_every: int = 5,
    holdout_frac: float = 0.3,
    flow_halflife_s: float = 90.0,
) -> FlowCalibrationReport:
    rows = load_journal(journal)
    if no_token:
        filtered = filter_rows_for_tokens(rows, yes_token=yes_token, no_token=no_token)
        if filtered:
            rows = filtered
    cut = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - holdout_frac)))))
    tune_rows = rows[:cut] if holdout_frac > 0 else rows
    hold_rows = rows[cut:] if holdout_frac > 0 else rows

    tune = _collect(tune_rows, yes_token, sample_every=sample_every, flow_halflife_s=flow_halflife_s)
    hold = _collect(hold_rows, yes_token, sample_every=sample_every, flow_halflife_s=flow_halflife_s)
    _, t_y = _pair(tune, horizon_s)
    clim = (sum(t_y) / len(t_y)) if t_y else 0.5
    probs, ys = _pair(hold, horizon_s)
    scored = _score_binary(probs, ys, label="flow_z_direction", climatology=clim)
    finding = bool(
        scored.get("beats_baseline")
        and scored.get("ci_excludes_zero")
        and scored.get("is_significant")
    )
    return FlowCalibrationReport(
        n=len(probs),
        horizon_s=horizon_s,
        flow=scored,
        verdict={
            "flow_finding": finding,
            "climatology": clim,
            "holdout_frac": holdout_frac,
            "flow_halflife_s": flow_halflife_s,
            "note": "finding requires Brier better than tune climatology, CI>0 skill, p<0.05",
        },
    )


def write_flow_report(report: FlowCalibrationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
