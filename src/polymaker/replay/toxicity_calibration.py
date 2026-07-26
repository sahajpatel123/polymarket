"""Passive-side toxicity calibration (virtual markouts after public trades).

After each aggressor trade, treat the passive side as a virtual maker fill and
update MarkoutTracker. Map current toxicity to P(big |Δmid|) and score on OOS
holdout vs tune climatology.

Eval infra only — does not change quote math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.domain import Side
from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_last_trade, parse_price_changes
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.replay.signal_calibration import _score_binary
from polymaker.strategy.estimators import MarkoutTracker


def toxicity_to_prob(tox: float, *, scale: float = 50.0) -> float:
    """Map non-negative toxicity to P(big move) in (0, 1)."""
    x = max(0.0, float(tox) * float(scale))
    # 1 - exp(-x) saturates in [0, 1)
    return max(0.0, min(1.0, 1.0 - (2.718281828 ** (-x))))


def _mid(book: OrderBook) -> float | None:
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None or bb.price >= ba.price:
        return None
    return 0.5 * (bb.price + ba.price)


@dataclass(frozen=True)
class ToxSample:
    ts: float
    mid: float
    prob: float


def _collect(
    rows: list[dict[str, Any]],
    yes_token: str,
    *,
    sample_every: int,
    markout_horizon_s: float,
) -> list[ToxSample]:
    book = OrderBook()
    mt = MarkoutTracker(horizon_s=markout_horizon_s, ewma_halflife_s=300.0)
    out: list[ToxSample] = []
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
            mid = _mid(book) or float(tp.price)
            # Passive maker is opposite the aggressor
            our = Side.SELL if tp.aggressor is Side.BUY else Side.BUY
            mt.record_fill(our, mid, float(tp.ts or ts))
            mt.evaluate(mid, float(tp.ts or ts))
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
        mt.evaluate(mid, ts)
        n += 1
        if sample_every > 1 and (n % sample_every) != 0:
            continue
        out.append(ToxSample(ts=ts, mid=mid, prob=toxicity_to_prob(mt.toxicity)))
    return out


def _pair(
    samples: list[ToxSample], horizon_s: float, *, move_threshold: float | None
) -> tuple[list[float], list[float], float]:
    abs_rets: list[float] = []
    pairs: list[tuple[ToxSample, float]] = []
    j = 0
    for s in samples:
        target = s.ts + horizon_s
        while j < len(samples) and samples[j].ts < target:
            j += 1
        if j >= len(samples):
            break
        ret = abs(samples[j].mid - s.mid)
        abs_rets.append(ret)
        pairs.append((s, ret))
    if not pairs:
        return [], [], 0.0
    thr = float(move_threshold) if move_threshold is not None else sorted(abs_rets)[len(abs_rets) // 2]
    probs = [s.prob for s, _ in pairs]
    ys = [1.0 if r > thr else 0.0 for _, r in pairs]
    return probs, ys, thr


@dataclass(frozen=True)
class ToxicityCalibrationReport:
    n: int
    horizon_s: float
    toxicity: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "horizon_s": self.horizon_s,
            "toxicity": self.toxicity,
            "verdict": self.verdict,
        }


def calibrate_toxicity(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    horizon_s: float = 30.0,
    sample_every: int = 5,
    holdout_frac: float = 0.3,
    markout_horizon_s: float = 30.0,
) -> ToxicityCalibrationReport:
    rows = load_journal(journal)
    if no_token:
        filtered = filter_rows_for_tokens(rows, yes_token=yes_token, no_token=no_token)
        if filtered:
            rows = filtered
    cut = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - holdout_frac)))))
    tune_rows = rows[:cut] if holdout_frac > 0 else rows
    hold_rows = rows[cut:] if holdout_frac > 0 else rows

    tune = _collect(
        tune_rows, yes_token, sample_every=sample_every, markout_horizon_s=markout_horizon_s
    )
    _, t_y, thr = _pair(tune, horizon_s, move_threshold=None)
    clim = (sum(t_y) / len(t_y)) if t_y else 0.5

    hold = _collect(
        hold_rows, yes_token, sample_every=sample_every, markout_horizon_s=markout_horizon_s
    )
    probs, ys, thr_used = _pair(hold, horizon_s, move_threshold=thr)
    scored = _score_binary(probs, ys, label="toxicity_big_move", climatology=clim)
    scored["move_threshold"] = thr_used
    finding = bool(
        scored.get("beats_baseline")
        and scored.get("ci_excludes_zero")
        and scored.get("is_significant")
    )
    return ToxicityCalibrationReport(
        n=len(probs),
        horizon_s=horizon_s,
        toxicity=scored,
        verdict={
            "toxicity_finding": finding,
            "climatology": clim,
            "holdout_frac": holdout_frac,
            "note": "finding requires Brier better than tune climatology, CI>0 skill, p<0.05",
        },
    )


def write_toxicity_report(report: ToxicityCalibrationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
