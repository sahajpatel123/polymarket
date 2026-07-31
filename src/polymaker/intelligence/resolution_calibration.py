"""Calibration tracker for resolution probability estimates.

Every resolved market provides a training example:
    (P_estimate, market_price, actual_outcome)

Over time, systematic biases in the LLM's probability estimates can be
detected and corrected. The core metric is Expected Calibration Error
(ECE): the average |P_estimate - observed_frequency| across bins.

When ECE > 10%, a Platt-scaled correction is applied before the estimate
is used for resolution arbitrage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalibrationRecord:
    """One resolved market — the ground truth for calibration."""

    condition_id: str
    estimated_p: float  # what the LLM estimated P(YES)
    market_price: float  # market price at time of estimate
    actual_outcome: bool  # True = resolved YES, False = NO
    estimate_timestamp: float = 0.0
    resolve_timestamp: float = 0.0
    confidence: float = 0.0  # LLM's self-reported confidence


@dataclass
class ResolutionCalibrator:
    """Calibration tracker and bias corrector for resolution estimates.

    Maintains a store of calibration records and provides:
    - ECE (Expected Calibration Error) — how miscalibrated the LLM is
    - calibrated_p(p_estimate) — bias-corrected probability
    - record_outcome(...) — add a resolved market to the training set

    Bias correction uses Platt scaling (logistic regression on the logit
    of the estimate), which is the standard calibration method for
    classifier probabilities. When fewer than 10 records exist, no
    correction is applied (not enough data).
    """

    records: list[CalibrationRecord] = field(default_factory=list)
    _alpha: float = 0.0  # Platt scaling intercept
    _beta: float = 1.0  # Platt scaling slope
    _calibrated: bool = False

    # ── record keeping ───────────────────────────────────────────────

    def record_outcome(
        self,
        condition_id: str,
        estimated_p: float,
        market_price: float,
        actual_outcome: bool,
        *,
        confidence: float = 0.0,
        estimate_timestamp: float = 0.0,
        resolve_timestamp: float = 0.0,
    ) -> None:
        self.records.append(CalibrationRecord(
            condition_id=condition_id,
            estimated_p=estimated_p,
            market_price=market_price,
            actual_outcome=actual_outcome,
            confidence=confidence,
            estimate_timestamp=estimate_timestamp,
            resolve_timestamp=resolve_timestamp,
        ))
        self._calibrated = False  # invalidate cached calibration

    @property
    def n_records(self) -> int:
        return len(self.records)

    # ── Expected Calibration Error ───────────────────────────────────

    def compute_ece(self, n_bins: int = 10) -> float:
        """Expected Calibration Error: |P_estimate - accuracy| per bin.

        Bins are equally spaced in [0, 1]. A perfectly calibrated
        estimator has ECE = 0.
        """
        if not self.records:
            return 0.0
        bin_edges = [i / n_bins for i in range(n_bins + 1)]
        ece = 0.0
        n_total = len(self.records)
        for i in range(n_bins):
            lo, hi = bin_edges[i], bin_edges[i + 1]
            in_bin = [r for r in self.records if lo <= r.estimated_p < hi]
            if not in_bin:
                continue
            avg_p = sum(r.estimated_p for r in in_bin) / len(in_bin)
            pos_freq = sum(1 for r in in_bin if r.actual_outcome) / len(in_bin)
            ece += abs(avg_p - pos_freq) * len(in_bin) / n_total
        return ece

    # ── Platt scaling ────────────────────────────────────────────────

    def _fit_platt(self) -> None:
        """Fit Platt scaling via Newton-Raphson on the logit of P.

        Maps P → 1 / (1 + exp(-(alpha + beta * logit(P)))).

        Simplified: uses the closed-form approximation when the number
        of positive and negative samples is roughly balanced.
        """
        pos = sum(1 for r in self.records if r.actual_outcome)
        neg = len(self.records) - pos
        if pos == 0 or neg == 0:
            self._alpha = 0.0
            self._beta = 1.0
            self._calibrated = True
            return

        # Compute log-odds of each estimate
        logits = []
        targets = []
        for r in self.records:
            p = max(min(r.estimated_p, 0.999), 0.001)  # avoid log(0)
            logit = math.log(p / (1.0 - p))
            logits.append(logit)
            targets.append(1.0 if r.actual_outcome else 0.0)

        # Simple linear regression: target ~ alpha + beta * logit
        n = len(logits)
        sx = sum(logits)
        sy = sum(targets)
        sxx = sum(x * x for x in logits)
        sxy = sum(x * y for x, y in zip(logits, targets, strict=False))

        denom = n * sxx - sx * sx
        if abs(denom) < 1e-12:
            self._alpha = 0.0
            self._beta = 1.0
        else:
            self._beta = (n * sxy - sx * sy) / denom
            self._alpha = (sy - self._beta * sx) / n
            # Clamp to prevent extreme corrections
            self._beta = max(0.1, min(10.0, self._beta))
            self._alpha = max(-5.0, min(5.0, self._alpha))

        self._calibrated = True

    def calibrated_p(self, p_estimate: float) -> float:
        """Return a bias-corrected probability estimate.

        Uses Platt scaling when >= 10 calibration records exist.
        Falls back to the raw estimate otherwise.
        """
        if len(self.records) < 10:
            return p_estimate
        if not self._calibrated:
            self._fit_platt()
        p = max(min(p_estimate, 0.999), 0.001)
        logit = math.log(p / (1.0 - p))
        calibrated = 1.0 / (1.0 + math.exp(-(self._alpha + self._beta * logit)))
        return max(0.01, min(0.99, calibrated))

    # ── summary ──────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        ece = self.compute_ece()
        return {
            "n_records": self.n_records,
            "ece": round(ece, 4),
            "ece_pct": f"{ece * 100:.1f}%",
            "alpha": round(self._alpha, 3),
            "beta": round(self._beta, 3),
            "calibrated": self._calibrated,
            "needs_calibration": len(self.records) >= 10 and ece > 0.10,
        }
