"""Fair-value predictor calibration: mid vs microprice vs Kalman.

Scores short-horizon price forecasts with quadratic error (proper for
continuous [0,1] targets), OOS holdout, and paired significance of
skill vs naive mid. Also scores a calibration-weighted blend of
microprice + Kalman using historical Brier/MSE weights from the tune window.

Eval infra only — does not change quote math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polymaker.intelligence.signal_processing import KalmanMidPrice
from polymaker.marketdata.orderbook import OrderBook
from polymaker.marketdata.parse import parse_book, parse_last_trade, parse_price_changes
from polymaker.replay import filter_rows_for_tokens, load_journal
from polymaker.strategy.calibration import (
    bootstrap_confidence_interval,
    paired_significance_test,
)
from polymaker.strategy.estimators import FlowEstimator
from polymaker.strategy.quoting import compute_fair_value
from polymaker.strategy.signal_blend import SignalSource, blend_probabilities, calibration_weight


@dataclass(frozen=True)
class FVSample:
    ts: float
    mid: float
    micro: float
    kalman: float
    blend: float
    micro_flow: float  # micro + live flow nudge (weight=0.5)


@dataclass(frozen=True)
class FVCalibrationReport:
    n: int
    horizon_s: float
    predictors: dict[str, Any]
    pairwise: dict[str, Any]
    verdict: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "horizon_s": self.horizon_s,
            "predictors": self.predictors,
            "pairwise": self.pairwise,
            "verdict": self.verdict,
        }


def _mid(book: OrderBook) -> float | None:
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None or bb.price >= ba.price:
        return None
    return 0.5 * (bb.price + ba.price)


def _collect(
    rows: list[dict[str, Any]],
    yes_token: str,
    *,
    sample_every: int,
    micro_levels: int,
    micro_brier: float,
    kalman_brier: float,
    flow_halflife_s: float = 90.0,
    flow_weight: float = 0.5,
) -> list[FVSample]:
    book = OrderBook()
    kalman = KalmanMidPrice(Q=1e-6, R=1e-5)
    flow = FlowEstimator(flow_halflife_s)
    out: list[FVSample] = []
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
        micro = book.microprice(micro_levels)
        if micro is None:
            continue
        tick = float(book.tick_size or 0.01)
        flow.decay_to(ts)
        k_hat, _ = kalman.update(mid)
        blend = blend_probabilities(
            (
                SignalSource("micro", micro, micro_brier),
                SignalSource("kalman", k_hat, kalman_brier),
            )
        ).probability
        micro_flow = compute_fair_value(float(micro), flow.z, tick, weight=flow_weight)

        n += 1
        if sample_every > 1 and (n % sample_every) != 0:
            continue
        out.append(
            FVSample(
                ts=ts,
                mid=mid,
                micro=float(micro),
                kalman=float(k_hat),
                blend=float(blend),
                micro_flow=float(micro_flow),
            )
        )
    return out


def _pair_errors(
    samples: list[FVSample], horizon_s: float
) -> dict[str, list[float]]:
    """Squared errors vs future mid for each predictor."""
    keys = ("mid", "micro", "kalman", "blend", "micro_flow")
    errs: dict[str, list[float]] = {k: [] for k in keys}
    j = 0
    for s in samples:
        target = s.ts + horizon_s
        while j < len(samples) and samples[j].ts < target:
            j += 1
        if j >= len(samples):
            break
        y = samples[j].mid
        preds = {
            "mid": s.mid,
            "micro": s.micro,
            "kalman": s.kalman,
            "blend": s.blend,
            "micro_flow": s.micro_flow,
        }
        for k, p in preds.items():
            errs[k].append((p - y) ** 2)
    return errs


def _mse_report(errors: list[float], *, label: str) -> dict[str, Any]:
    if len(errors) < 10:
        return {"label": label, "n": len(errors), "status": "insufficient_samples", "mse": None}
    mse = sum(errors) / len(errors)
    mean, lo, hi = bootstrap_confidence_interval(errors, n_resamples=800, seed=29)
    return {
        "label": label,
        "n": len(errors),
        "status": "ok",
        "mse": round(mse, 12),
        "bootstrap_mean": mean,
        "bootstrap_ci_lower": lo,
        "bootstrap_ci_upper": hi,
    }


def _skill_vs_mid(
    mid_err: list[float], cand_err: list[float], *, label: str
) -> dict[str, Any]:
    if len(mid_err) < 10 or len(mid_err) != len(cand_err):
        return {
            "label": label,
            "n": min(len(mid_err), len(cand_err)),
            "finding": False,
            "status": "insufficient_samples",
        }
    # Positive skill = candidate better (lower SE) than mid
    deltas = [m - c for m, c in zip(mid_err, cand_err)]
    mean, lo, hi = bootstrap_confidence_interval(deltas, n_resamples=1000, seed=31)
    paired = paired_significance_test(mid_err, cand_err, alpha=0.05)
    skill = -float(paired.mean_delta)
    ci_ok = abs(hi - lo) > 1e-18 and lo > 0.0
    finding = bool(skill > 0 and paired.p_value < 0.05 and ci_ok)
    return {
        "label": label,
        "n": len(deltas),
        "status": "ok",
        "bootstrap_mean_skill": mean,
        "bootstrap_ci_lower": lo,
        "bootstrap_ci_upper": hi,
        "paired_p": paired.p_value,
        "beats_mid": skill > 0,
        "ci_excludes_zero": ci_ok,
        "is_significant": paired.p_value < 0.05 and skill > 0,
        "finding": finding,
    }


def _tune_briers(errs: dict[str, list[float]]) -> tuple[float, float]:
    """Map tune-window MSE to pseudo-Brier for blend weights (clip to [0, 0.25])."""
    def _b(key: str) -> float:
        e = errs.get(key) or []
        if len(e) < 5:
            return 0.25
        mse = sum(e) / len(e)
        # Scale tiny price MSE into [0, 0.25] for weight mapping; floor at eps
        return min(0.25, max(1e-6, mse * 1000.0))

    return _b("micro"), _b("kalman")


def calibrate_fair_value(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    horizon_s: float = 30.0,
    sample_every: int = 5,
    holdout_frac: float = 0.3,
    micro_levels: int = 3,
) -> FVCalibrationReport:
    rows = load_journal(journal)
    if no_token:
        filtered = filter_rows_for_tokens(rows, yes_token=yes_token, no_token=no_token)
        if filtered:
            rows = filtered

    cut = max(1, min(len(rows) - 1, int(round(len(rows) * (1.0 - holdout_frac)))))
    tune_rows = rows[:cut] if holdout_frac > 0 else rows
    hold_rows = rows[cut:] if holdout_frac > 0 else rows

    # First pass: equal weights to estimate tune MSE → blend weights
    tune0 = _collect(
        tune_rows, yes_token, sample_every=sample_every, micro_levels=micro_levels,
        micro_brier=0.12, kalman_brier=0.12,
    )
    tune_errs = _pair_errors(tune0, horizon_s)
    micro_b, kalman_b = _tune_briers(tune_errs)

    hold = _collect(
        hold_rows, yes_token, sample_every=sample_every, micro_levels=micro_levels,
        micro_brier=micro_b, kalman_brier=kalman_b,
    )
    errs = _pair_errors(hold, horizon_s)

    predictors = {k: _mse_report(v, label=k) for k, v in errs.items()}
    pairwise = {
        "micro_vs_mid": _skill_vs_mid(errs["mid"], errs["micro"], label="micro_vs_mid"),
        "kalman_vs_mid": _skill_vs_mid(errs["mid"], errs["kalman"], label="kalman_vs_mid"),
        "blend_vs_mid": _skill_vs_mid(errs["mid"], errs["blend"], label="blend_vs_mid"),
        "micro_vs_kalman": _skill_vs_mid(errs["kalman"], errs["micro"], label="micro_vs_kalman"),
        "micro_flow_vs_mid": _skill_vs_mid(errs["mid"], errs["micro_flow"], label="micro_flow_vs_mid"),
        "micro_flow_vs_micro": _skill_vs_mid(
            errs["micro"], errs["micro_flow"], label="micro_flow_vs_micro"
        ),
    }
    verdict = {
        "micro_finding": bool(pairwise["micro_vs_mid"].get("finding")),
        "kalman_finding": bool(pairwise["kalman_vs_mid"].get("finding")),
        "blend_finding": bool(pairwise["blend_vs_mid"].get("finding")),
        "micro_flow_finding": bool(pairwise["micro_flow_vs_mid"].get("finding")),
        "flow_nudge_helps_micro": bool(pairwise["micro_flow_vs_micro"].get("finding")),
        "any_finding": any(
            pairwise[k].get("finding")
            for k in ("micro_vs_mid", "kalman_vs_mid", "blend_vs_mid", "micro_flow_vs_mid")
        ),
        "tune_micro_brier_proxy": micro_b,
        "tune_kalman_brier_proxy": kalman_b,
        "tune_micro_weight": calibration_weight(micro_b),
        "tune_kalman_weight": calibration_weight(kalman_b),
        "holdout_frac": holdout_frac,
        "n_holdout_events": len(hold_rows),
        "note": "finding=true if predictor beats mid on OOS MSE with CI>0 skill and p<0.05",
    }
    return FVCalibrationReport(
        n=len(errs["mid"]),
        horizon_s=horizon_s,
        predictors=predictors,
        pairwise=pairwise,
        verdict=verdict,
    )


def calibrate_fair_value_multi_horizon(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    horizons_s: tuple[float, ...] = (5.0, 30.0, 120.0),
    sample_every: int = 5,
    holdout_frac: float = 0.3,
    micro_levels: int = 3,
) -> dict[str, Any]:
    """Run FV calibration across several horizons; summarize micro findings."""
    by_h: dict[str, Any] = {}
    for h in horizons_s:
        rep = calibrate_fair_value(
            journal,
            yes_token=yes_token,
            no_token=no_token,
            horizon_s=float(h),
            sample_every=sample_every,
            holdout_frac=holdout_frac,
            micro_levels=micro_levels,
        )
        d = rep.as_dict()
        by_h[str(h)] = {
            "n": d["n"],
            "micro_finding": d["verdict"].get("micro_finding"),
            "kalman_finding": d["verdict"].get("kalman_finding"),
            "blend_finding": d["verdict"].get("blend_finding"),
            "mse_mid": (d["predictors"].get("mid") or {}).get("mse"),
            "mse_micro": (d["predictors"].get("micro") or {}).get("mse"),
            "micro_vs_mid": d["pairwise"].get("micro_vs_mid"),
        }
    micro_wins = [h for h, v in by_h.items() if v.get("micro_finding")]
    return {
        "horizons_s": list(horizons_s),
        "by_horizon": by_h,
        "micro_win_horizons": micro_wins,
        "micro_any_horizon": len(micro_wins) > 0,
        "micro_all_horizons": len(micro_wins) == len(horizons_s),
    }


def sweep_micro_levels(
    journal: Path,
    *,
    yes_token: str,
    no_token: str | None = None,
    levels: tuple[int, ...] = (1, 2, 3, 5, 8),
    horizon_s: float = 30.0,
    sample_every: int = 5,
    holdout_frac: float = 0.3,
) -> dict[str, Any]:
    """Compare microprice depth (levels) on OOS skill vs mid."""
    rows_out: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    for lv in levels:
        rep = calibrate_fair_value(
            journal,
            yes_token=yes_token,
            no_token=no_token,
            horizon_s=horizon_s,
            sample_every=sample_every,
            holdout_frac=holdout_frac,
            micro_levels=int(lv),
        )
        d = rep.as_dict()
        mv = d["pairwise"].get("micro_vs_mid") or {}
        row = {
            "micro_levels": lv,
            "n": d["n"],
            "mse_mid": (d["predictors"].get("mid") or {}).get("mse"),
            "mse_micro": (d["predictors"].get("micro") or {}).get("mse"),
            "micro_finding": d["verdict"].get("micro_finding"),
            "bootstrap_mean_skill": mv.get("bootstrap_mean_skill"),
            "paired_p": mv.get("paired_p"),
            "ci_excludes_zero": mv.get("ci_excludes_zero"),
        }
        rows_out.append(row)
        if row["micro_finding"] and row["mse_micro"] is not None:
            if best is None or float(row["mse_micro"]) < float(best["mse_micro"]):
                best = row
    return {
        "horizon_s": horizon_s,
        "levels": list(levels),
        "rows": rows_out,
        "best_finding_row": best,
        "any_finding": any(r.get("micro_finding") for r in rows_out),
    }


def write_fv_report(report: FVCalibrationReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n")
    return path
