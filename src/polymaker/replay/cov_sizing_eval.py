"""Multi-market covariance sizing evidence from a shared journal.

Aligns mid returns across two YES tokens, estimates covariance on a tune
window, then on holdout measures portfolio variance of equal independent
notionals vs covariance-scaled notionals. Reports bootstrap CI on the
variance reduction.

Eval infra only — does not change quote math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_price_changes
from polymaker.replay import load_journal
from polymaker.strategy.calibration import bootstrap_confidence_interval
from polymaker.strategy.covariance_sizing import (
    compute_covariance_matrix,
    scale_correlated_positions,
)


def _mid(book: OrderBook) -> float | None:
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None or bb.price >= ba.price:
        return None
    return 0.5 * (bb.price + ba.price)


def collect_aligned_returns(
    rows: list[dict[str, Any]],
    token_a: str,
    token_b: str,
    *,
    sample_every: int = 5,
) -> tuple[list[float], list[float]]:
    books = {token_a: OrderBook(), token_b: OrderBook()}
    last: dict[str, float | None] = {token_a: None, token_b: None}
    out_a: list[float] = []
    out_b: list[float] = []
    n = 0

    for row in rows:
        kind = row.get("kind")
        data = row.get("data")
        ts = float(row.get("ts") or 0.0)
        if not isinstance(data, dict):
            continue
        touched: set[str] = set()
        if kind == "book":
            upd = parse_book(data)
            if upd is None or upd.asset_id not in books:
                continue
            if upd.tick_size:
                books[upd.asset_id].set_tick_size(upd.tick_size)
            books[upd.asset_id].apply_snapshot(
                upd.bids, upd.asks, upd.ts or ts, upd.book_hash
            )
            touched.add(upd.asset_id)
        elif kind == "price_change":
            for ch in parse_price_changes(data):
                if ch.asset_id not in books:
                    continue
                books[ch.asset_id].apply_delta(ch.side, ch.price, ch.size, ch.ts or ts)
                touched.add(ch.asset_id)
        else:
            continue
        if not touched:
            continue

        mids = {t: _mid(books[t]) for t in (token_a, token_b)}
        if mids[token_a] is None or mids[token_b] is None:
            continue
        n += 1
        if sample_every > 1 and (n % sample_every) != 0:
            continue
        if last[token_a] is not None and last[token_b] is not None:
            out_a.append(float(mids[token_a]) - float(last[token_a]))
            out_b.append(float(mids[token_b]) - float(last[token_b]))
        last[token_a] = float(mids[token_a])
        last[token_b] = float(mids[token_b])
    return out_a, out_b


@dataclass(frozen=True)
class CovSizingReport:
    n_tune: int
    n_holdout: int
    corr_tune: float
    corr_holdout: float
    cov_tune: list[list[float]]
    independent_var: float
    scaled_var: float
    scaling_factor: float
    variance_reduction: float
    bootstrap: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_tune": self.n_tune,
            "n_holdout": self.n_holdout,
            "corr_tune": self.corr_tune,
            "corr_holdout": self.corr_holdout,
            "cov_tune": self.cov_tune,
            "independent_var": self.independent_var,
            "scaled_var": self.scaled_var,
            "scaling_factor": self.scaling_factor,
            "variance_reduction": self.variance_reduction,
            "bootstrap": self.bootstrap,
            "verdict": self.verdict,
        }


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    if da < 1e-18 or db < 1e-18:
        return 0.0
    return num / (da * db)


def evaluate_covariance_sizing(
    journal: Path,
    *,
    token_a: str,
    token_b: str,
    notional: float = 100.0,
    max_portfolio_variance: float | None = None,
    holdout_frac: float = 0.3,
    sample_every: int = 5,
) -> CovSizingReport:
    rows = load_journal(journal)
    cut = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - holdout_frac)))))
    tune_rows = rows[:cut] if holdout_frac > 0 else rows
    hold_rows = rows[cut:] if holdout_frac > 0 else rows

    ta, tb = collect_aligned_returns(tune_rows, token_a, token_b, sample_every=sample_every)
    ha, hb = collect_aligned_returns(hold_rows, token_a, token_b, sample_every=sample_every)
    cov = compute_covariance_matrix([ta, tb])
    q = [notional, notional]
    # Uncorrelated budget: sum of marginal variances (no cross term).
    # Positive correlation then pushes port var above cap → downscale.
    uncorr_var = q[0] * cov[0][0] * q[0] + q[1] * cov[1][1] * q[1]
    ind_var = uncorr_var + 2 * q[0] * cov[0][1] * q[1]
    cap = (
        max_portfolio_variance
        if max_portfolio_variance is not None
        else max(uncorr_var, 1e-18)
    )
    scaled = scale_correlated_positions(q, cov, cap)
    corr_t = _corr(ta, tb)
    corr_h = _corr(ha, hb)

    # Holdout realized portfolio variance of independent vs scaled notionals
    # using rolling block returns (paired).
    def _port_vars(ra: list[float], rb: list[float], notionals: tuple[float, float]) -> list[float]:
        # Per-step contribution approx (q_a r_a + q_b r_b)^2
        out = []
        for x, y in zip(ra, rb):
            pr = notionals[0] * x + notionals[1] * y
            out.append(pr * pr)
        return out

    ind_series = _port_vars(ha, hb, (notional, notional))
    sc_series = _port_vars(ha, hb, scaled.adjusted_notionals)
    reductions = [i - s for i, s in zip(ind_series, sc_series)]
    if reductions:
        mean, lo, hi = bootstrap_confidence_interval(reductions, n_resamples=1000, seed=41)
        ci_ok = abs(hi - lo) > 1e-18 and lo > 0.0
        finding = bool(
            mean > 0
            and ci_ok
            and scaled.scaling_factor < 0.999
            and abs(corr_t) >= 0.2
        )
        boot = {
            "mean_reduction": mean,
            "ci_lower": lo,
            "ci_upper": hi,
            "ci_excludes_zero": ci_ok,
            "n": len(reductions),
        }
    else:
        finding = False
        boot = {"n": 0, "ci_excludes_zero": False, "mean_reduction": 0.0}

    var_red = ind_var - scaled.portfolio_variance if ind_var > 0 else 0.0
    verdict = {
        "finding": finding,
        "material_correlation": abs(corr_t) >= 0.2 or abs(corr_h) >= 0.2,
        "scaled": scaled.scaling_factor < 0.999,
        "note": (
            "finding=true when tune |corr|>=0.2 triggers downscale vs uncorrelated "
            "budget and holdout realized variance falls with bootstrap CI>0"
        ),
    }
    return CovSizingReport(
        n_tune=len(ta),
        n_holdout=len(ha),
        corr_tune=round(corr_t, 6),
        corr_holdout=round(corr_h, 6),
        cov_tune=[[round(x, 12) for x in row] for row in cov],
        independent_var=round(ind_var, 12),
        scaled_var=round(scaled.portfolio_variance, 12),
        scaling_factor=scaled.scaling_factor,
        variance_reduction=round(var_red, 12),
        bootstrap=boot,
        verdict=verdict,
    )


def write_cov_report(report: CovSizingReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
