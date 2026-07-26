"""Signal calibration: proper scoring of OFI / VPIN / Kyle predictors.

Walks a journal (books + trades), feeds the quote-neutral estimators, and
scores short-horizon forecasts with Brier/log-loss vs an uninformative
baseline — plus bootstrap CI on the Brier delta.

This is eval infra only. It does not change quote math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_last_trade, parse_price_changes
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.strategy.calibration import (
    bootstrap_confidence_interval,
    brier_score,
    evaluate_calibration,
    log_loss,
    paired_significance_test,
)
from polymaker.strategy.kyle_lambda import KyleLambdaEstimator
from polymaker.strategy.ofi import OFICalculator
from polymaker.strategy.vpin import VPINEstimator


def ofi_to_prob_up(normalized_ofi: float) -> float:
    """Map OFI in [-1, 1] to P(mid up). 0 → 0.5 (uninformative)."""
    x = max(-1.0, min(1.0, float(normalized_ofi)))
    return 0.5 + 0.5 * x


def vpin_to_prob_move(vpin: float) -> float:
    """Map VPIN in [0, 1] to P(|Δmid| exceeds sample median later)."""
    return max(0.0, min(1.0, float(vpin)))


@dataclass(frozen=True)
class SignalSample:
    ts: float
    mid: float
    ofi_prob: float
    vpin_prob: float
    kyle_lambda: float


@dataclass(frozen=True)
class SignalCalibrationReport:
    n_samples: int
    horizon_s: float
    ofi: dict[str, Any]
    vpin: dict[str, Any]
    kyle: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_samples": self.n_samples,
            "horizon_s": self.horizon_s,
            "ofi": self.ofi,
            "vpin": self.vpin,
            "kyle": self.kyle,
            "verdict": self.verdict,
        }


def _mid_from_book(book: OrderBook) -> float | None:
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None or bb.price >= ba.price:
        return None
    return 0.5 * (bb.price + ba.price)


def _collect_samples(
    rows: list[dict[str, Any]],
    *,
    yes_token: str,
    sample_every: int = 1,
) -> list[SignalSample]:
    book = OrderBook()
    ofi = OFICalculator(halflife_s=30.0)
    vpin = VPINEstimator(bucket_volume=50.0, n_buckets=20)
    kyle = KyleLambdaEstimator(halflife_s=300.0)
    samples: list[SignalSample] = []
    n_book = 0

    for row in rows:
        kind = row.get("kind")
        data = row.get("data")
        ts = float(row.get("ts") or 0.0)
        if not isinstance(data, dict):
            continue

        if kind == "book":
            upd = parse_book(data)
            if upd is None or upd.asset_id != yes_token:
                continue
            if upd.tick_size:
                book.set_tick_size(upd.tick_size)
            book.apply_snapshot(upd.bids, upd.asks, upd.ts or ts, upd.book_hash)
            mid = _mid_from_book(book)
            if mid is None:
                continue
            view = book.view()
            ofi.update_from_book(view, ts)
            n_book += 1
            if sample_every > 1 and (n_book % sample_every) != 0:
                continue
            samples.append(
                SignalSample(
                    ts=ts,
                    mid=mid,
                    ofi_prob=ofi_to_prob_up(ofi.normalized_ofi),
                    vpin_prob=vpin_to_prob_move(vpin.vpin),
                    kyle_lambda=kyle.lambda_param,
                )
            )
        elif kind == "price_change":
            for ch in parse_price_changes(data):
                if ch.asset_id != yes_token:
                    continue
                book.apply_delta(ch.side, ch.price, ch.size, ch.ts or ts)
            mid = _mid_from_book(book)
            if mid is None:
                continue
            view = book.view()
            ofi.update_from_book(view, ts)
            n_book += 1
            if sample_every > 1 and (n_book % sample_every) != 0:
                continue
            samples.append(
                SignalSample(
                    ts=ts,
                    mid=mid,
                    ofi_prob=ofi_to_prob_up(ofi.normalized_ofi),
                    vpin_prob=vpin_to_prob_move(vpin.vpin),
                    kyle_lambda=kyle.lambda_param,
                )
            )
        elif kind == "last_trade_price":
            tp = parse_last_trade(data)
            if tp is None or tp.asset_id != yes_token:
                continue
            mid = _mid_from_book(book) or float(tp.price)
            vpin.update(tp.aggressor, tp.size)
            kyle.update(mid=mid, aggressor=tp.aggressor, size=tp.size, ts=float(tp.ts or ts))

    return samples


def _pair_outcomes(
    samples: list[SignalSample],
    horizon_s: float,
    *,
    move_threshold: float | None = None,
) -> tuple[list[float], list[float], list[float], list[float], float]:
    """Return ofi_probs, ofi_y, vpin_probs, vpin_y, move_threshold_used."""
    abs_rets: list[float] = []
    pairs: list[tuple[SignalSample, float]] = []
    j = 0
    for s in samples:
        target = s.ts + horizon_s
        while j < len(samples) and samples[j].ts < target:
            j += 1
        if j >= len(samples):
            break
        ret = samples[j].mid - s.mid
        abs_rets.append(abs(ret))
        pairs.append((s, ret))

    if not pairs:
        return [], [], [], [], 0.0

    thr = (
        float(move_threshold)
        if move_threshold is not None
        else sorted(abs_rets)[len(abs_rets) // 2]
    )
    ofi_p = [s.ofi_prob for s, _ in pairs]
    ofi_y = [1.0 if r > 0 else 0.0 for _, r in pairs]
    vpin_p = [s.vpin_prob for s, _ in pairs]
    vpin_y = [1.0 if abs(r) > thr else 0.0 for _, r in pairs]
    return ofi_p, ofi_y, vpin_p, vpin_y, thr


def _score_binary(
    probs: list[float],
    outcomes: list[float],
    *,
    label: str,
    climatology: float | None = None,
) -> dict[str, Any]:
    """Score probs vs outcomes; baseline is climatology (tune base rate) or 0.5."""
    if len(probs) < 10:
        return {
            "label": label,
            "n": len(probs),
            "status": "insufficient_samples",
            "brier": None,
            "brier_baseline": None,
            "delta_brier": None,
            "log_loss": None,
            "ece": None,
            "beats_baseline": False,
            "ci_excludes_zero": False,
            "is_significant": False,
            "climatology": climatology,
        }

    base_p = 0.5 if climatology is None else min(max(float(climatology), 1e-6), 1.0 - 1e-6)
    baseline = [base_p] * len(probs)
    bs = brier_score(probs, outcomes)
    bs0 = brier_score(baseline, outcomes)
    per = [(p - y) ** 2 for p, y in zip(probs, outcomes)]
    per0 = [(base_p - y) ** 2 for y in outcomes]
    deltas = [b0 - b for b0, b in zip(per0, per)]
    mean, lo, hi = bootstrap_confidence_interval(deltas, n_resamples=1000, seed=11)
    sig = paired_significance_test(per0, per, alpha=0.05)
    skill = -float(sig.mean_delta)
    cal = evaluate_calibration(probs, outcomes)
    ci_width = abs(hi - lo)
    ci_ok = ci_width > 1e-12 and lo > 0.0
    return {
        "label": label,
        "n": len(probs),
        "status": "ok",
        "brier": round(bs, 6),
        "brier_baseline": round(bs0, 6),
        "delta_brier": round(bs0 - bs, 6),
        "log_loss": cal.log_loss,
        "log_loss_baseline": round(log_loss(baseline, outcomes), 6),
        "ece": cal.expected_calibration_error,
        "climatology": round(base_p, 6),
        "bootstrap_mean_skill": mean,
        "bootstrap_ci_lower": lo,
        "bootstrap_ci_upper": hi,
        "paired_p": sig.p_value,
        "beats_baseline": (bs0 - bs) > 0,
        "ci_excludes_zero": ci_ok,
        "is_significant": bool(sig.p_value < 0.05 and skill > 0),
    }


def _kyle_absret_corr(
    samples: list[SignalSample], horizon_s: float
) -> dict[str, Any]:
    """Simple association: higher Kyle λ vs subsequent |Δmid| (Spearman-ish via ranks)."""
    xs: list[float] = []
    ys: list[float] = []
    j = 0
    for s in samples:
        target = s.ts + horizon_s
        while j < len(samples) and samples[j].ts < target:
            j += 1
        if j >= len(samples):
            break
        xs.append(s.kyle_lambda)
        ys.append(abs(samples[j].mid - s.mid))
    if len(xs) < 10:
        return {"n": len(xs), "status": "insufficient_samples", "corr": None}

    def _rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        for r, i in enumerate(order):
            ranks[i] = float(r)
        return ranks

    rx, ry = _rank(xs), _rank(ys)
    n = len(rx)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = sum((a - mx) ** 2 for a in rx) ** 0.5
    deny = sum((b - my) ** 2 for b in ry) ** 0.5
    corr = num / (denx * deny) if denx > 1e-12 and deny > 1e-12 else 0.0
    return {"n": n, "status": "ok", "spearman_absret": round(corr, 6)}


def calibrate_signals(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    horizon_s: float = 30.0,
    sample_every: int = 5,
    holdout_frac: float = 0.3,
) -> SignalCalibrationReport:
    rows = load_journal(journal)
    if no_token:
        filtered = filter_rows_for_tokens(rows, yes_token=yes_token, no_token=no_token)
        if filtered:
            rows = filtered

    cut = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - holdout_frac)))))
    tune_rows = rows[:cut] if holdout_frac > 0 else rows
    holdout_rows = rows[cut:] if holdout_frac > 0 else rows

    # Fit "big move" threshold + climatology on tune only (anti-leakage).
    tune_samples = _collect_samples(tune_rows, yes_token=yes_token, sample_every=sample_every)
    t_ofi_p, t_ofi_y, t_vpin_p, t_vpin_y, thr = _pair_outcomes(
        tune_samples, horizon_s, move_threshold=None
    )
    del t_ofi_p, t_vpin_p  # outcomes only needed for climatology
    ofi_clim = (sum(t_ofi_y) / len(t_ofi_y)) if t_ofi_y else 0.5
    vpin_clim = (sum(t_vpin_y) / len(t_vpin_y)) if t_vpin_y else 0.5

    samples = _collect_samples(holdout_rows, yes_token=yes_token, sample_every=sample_every)
    ofi_p, ofi_y, vpin_p, vpin_y, thr_used = _pair_outcomes(
        samples, horizon_s, move_threshold=thr
    )
    ofi_rep = _score_binary(ofi_p, ofi_y, label="ofi_direction", climatology=ofi_clim)
    vpin_rep = _score_binary(vpin_p, vpin_y, label="vpin_big_move", climatology=vpin_clim)
    vpin_rep["move_threshold"] = thr_used
    kyle_rep = _kyle_absret_corr(samples, horizon_s)

    ofi_ok = bool(
        ofi_rep.get("beats_baseline")
        and ofi_rep.get("ci_excludes_zero")
        and ofi_rep.get("is_significant")
    )
    vpin_ok = bool(
        vpin_rep.get("beats_baseline")
        and vpin_rep.get("ci_excludes_zero")
        and vpin_rep.get("is_significant")
    )
    verdict = {
        "ofi_finding": ofi_ok,
        "vpin_finding": vpin_ok,
        "any_finding": ofi_ok or vpin_ok,
        "holdout_frac": holdout_frac,
        "n_holdout_events": len(holdout_rows),
        "n_tune_events": len(tune_rows),
        "note": (
            "finding requires Brier better than tune climatology, bootstrap CI>0 skill, "
            "and paired p<0.05; VPIN move threshold + base rates fit on tune only"
        ),
    }
    return SignalCalibrationReport(
        n_samples=len(samples),
        horizon_s=horizon_s,
        ofi=ofi_rep,
        vpin=vpin_rep,
        kyle=kyle_rep,
        verdict=verdict,
    )


def write_signal_report(report: SignalCalibrationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
