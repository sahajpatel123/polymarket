"""Vol forecast calibration: GARCH(1,1) vs EWMA on squared returns.

Proper scoring for the "spreads should widen with vol" thesis: forecast
next-horizon realized variance and score with MSE (quadratic) plus
bootstrap CI on skill vs EWMA climatology/benchmark.

Eval infra only — does not change quote math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_price_changes
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.strategy.calibration import (
    bootstrap_confidence_interval,
    paired_significance_test,
)
from polymaker.strategy.estimators import Ewma
from polymaker.strategy.garch import GARCHVolatility


@dataclass(frozen=True)
class VolCalibrationReport:
    n: int
    horizon_s: float
    garch: dict[str, Any]
    ewma: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "horizon_s": self.horizon_s,
            "garch": self.garch,
            "ewma": self.ewma,
            "verdict": self.verdict,
        }


def _mid(book: OrderBook) -> float | None:
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None or bb.price >= ba.price:
        return None
    return 0.5 * (bb.price + ba.price)


def _mids_from_journal(
    rows: list[dict[str, Any]], yes_token: str, sample_every: int
) -> list[tuple[float, float]]:
    book = OrderBook()
    out: list[tuple[float, float]] = []
    n = 0
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
        elif kind == "price_change":
            touched = False
            for ch in parse_price_changes(data):
                if ch.asset_id != yes_token:
                    continue
                book.apply_delta(ch.side, ch.price, ch.size, ch.ts or ts)
                touched = True
            if not touched:
                continue
        elif kind == "last_trade_price":
            # trades don't update mid path here
            continue
        else:
            continue
        mid = _mid(book)
        if mid is None:
            continue
        n += 1
        if sample_every > 1 and (n % sample_every) != 0:
            continue
        out.append((ts, mid))
    return out


def _forecast_errors(
    series: list[tuple[float, float]],
    horizon_s: float,
) -> tuple[list[float], list[float]]:
    """Return (garch_se, ewma_se) squared-error series for variance forecasts."""
    garch = GARCHVolatility(omega=1e-10, alpha=0.08, beta=0.88)
    ewma = Ewma(halflife_s=60.0)
    g_err: list[float] = []
    e_err: list[float] = []

    # Build return series first
    rets: list[tuple[float, float]] = []  # (ts, r)
    for i in range(1, len(series)):
        t0, m0 = series[i - 1]
        t1, m1 = series[i]
        rets.append((t1, m1 - m0))

    j = 0
    for i, (ts, r) in enumerate(rets):
        garch.update(r)
        ewma.update(r * r, ts)
        # realized variance over next horizon_s of returns
        target = ts + horizon_s
        while j < len(rets) and rets[j][0] < target:
            j += 1
        if j <= i or j >= len(rets):
            continue
        # sum of squared returns in (ts, target]
        rv = 0.0
        k = i + 1
        while k < len(rets) and rets[k][0] <= target:
            rr = rets[k][1]
            rv += rr * rr
            k += 1
        if k == i + 1:
            continue
        g_hat = garch.variance
        e_hat = max(0.0, ewma.value)
        g_err.append((g_hat - rv) ** 2)
        e_err.append((e_hat - rv) ** 2)
    return g_err, e_err


def _score_mse(errors: list[float], *, label: str) -> dict[str, Any]:
    if len(errors) < 10:
        return {"label": label, "n": len(errors), "status": "insufficient_samples", "mse": None}
    mse = sum(errors) / len(errors)
    mean, lo, hi = bootstrap_confidence_interval(errors, n_resamples=800, seed=19)
    return {
        "label": label,
        "n": len(errors),
        "status": "ok",
        "mse": round(mse, 12),
        "bootstrap_mean": mean,
        "bootstrap_ci_lower": lo,
        "bootstrap_ci_upper": hi,
    }


def calibrate_vol_models(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    horizon_s: float = 30.0,
    sample_every: int = 5,
    holdout_frac: float = 0.3,
) -> VolCalibrationReport:
    rows = load_journal(journal)
    if no_token:
        filtered = filter_rows_for_tokens(rows, yes_token=yes_token, no_token=no_token)
        if filtered:
            rows = filtered
    cut = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - holdout_frac)))))
    holdout = rows[cut:] if holdout_frac > 0 else rows
    series = _mids_from_journal(holdout, yes_token, sample_every)
    g_err, e_err = _forecast_errors(series, horizon_s)
    g_rep = _score_mse(g_err, label="garch")
    e_rep = _score_mse(e_err, label="ewma")

    finding = False
    sig: dict[str, Any] = {"n": 0}
    if len(g_err) >= 10 and len(g_err) == len(e_err):
        # skill = ewma_err - garch_err (positive => garch better)
        deltas = [ee - ge for ge, ee in zip(g_err, e_err)]
        mean, lo, hi = bootstrap_confidence_interval(deltas, n_resamples=1000, seed=23)
        paired = paired_significance_test(e_err, g_err, alpha=0.05)
        # paired mean_delta = mean(g - e); garch better when mean_delta < 0
        skill = -float(paired.mean_delta)
        ci_ok = abs(hi - lo) > 1e-18 and lo > 0.0
        finding = bool(skill > 0 and paired.p_value < 0.05 and ci_ok)
        sig = {
            "n": len(deltas),
            "bootstrap_mean_skill": mean,
            "bootstrap_ci_lower": lo,
            "bootstrap_ci_upper": hi,
            "paired_p": paired.p_value,
            "garch_better": skill > 0,
            "ci_excludes_zero": ci_ok,
            "is_significant": paired.p_value < 0.05 and skill > 0,
        }

    verdict = {
        "garch_finding": finding,
        "holdout_frac": holdout_frac,
        "n_holdout_events": len(holdout),
        "significance": sig,
        "note": "finding=true only if GARCH MSE beats EWMA with CI>0 skill and p<0.05",
    }
    return VolCalibrationReport(
        n=len(g_err),
        horizon_s=horizon_s,
        garch=g_rep,
        ewma=e_rep,
        verdict=verdict,
    )


def write_vol_report(report: VolCalibrationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
