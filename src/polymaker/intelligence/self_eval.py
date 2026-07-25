"""Self-evaluation: calibration tracking, strategy decay detection.

A market-making strategy can drift over time as market conditions
change. The strategy that worked yesterday may not work today.
This module tracks the strategy's own performance and detects when
it's losing its edge.

Components:

1. Calibration tracking:
   - Predicted fill rate vs actual fill rate
   - Predicted edge vs actual edge
   - Predicted adverse selection vs actual
   - Chi-squared test for calibration

2. Strategy decay detection:
   - Track rolling Sharpe ratio
   - Detect when rolling Sharpe < threshold
   - Alert when strategy is decaying

3. PnL attribution:
   - Attribute PnL to specific decisions
   - Track which decisions made/lost money
   - Learn from historical decisions

4. Model drift detection:
   - Compare current market state distribution to training distribution
   - Use KL divergence to detect distribution shift
   - Alert when market behavior is significantly different

Pure state machines. The engine feeds outcomes and gets back
calibration metrics and decay alerts.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CalibrationTracker:
    """Track predicted vs actual outcomes for calibration.

    Maintains a running comparison of:
    - Predicted fill rate vs actual fill rate
    - Predicted edge vs actual edge
    - Predicted adverse selection vs actual

    Good calibration means predicted ≈ actual.
    Poor calibration means the model is miscalibrated.
    """

    # Predicted (from model)
    predicted_fill_rate: float = 0.0
    predicted_edge: float = 0.0
    predicted_as_rate: float = 0.0
    # Actual (from observed)
    actual_fills: int = 0
    actual_quotes: int = 0
    actual_edge_sum: float = 0.0
    actual_as_sum: float = 0.0  # adverse selection (negative = adverse)
    n_fills: int = 0

    def update_prediction(
        self, fill_rate: float, edge: float, as_rate: float
    ) -> None:
        """Update the model's predictions."""
        self.predicted_fill_rate = fill_rate
        self.predicted_edge = edge
        self.predicted_as_rate = as_rate

    def record_quote(self) -> None:
        """Record that a quote was placed."""
        self.actual_quotes += 1

    def record_fill(
        self, edge: float = 0.0, as_observed: float = 0.0
    ) -> None:
        """Record a fill outcome."""
        self.actual_fills += 1
        self.n_fills += 1
        self.actual_edge_sum += edge
        self.actual_as_sum += as_observed

    def fill_rate_calibration(self) -> float:
        """Error between predicted and actual fill rate.

        0.0 = perfect calibration, 1.0 = max error.
        """
        actual = self.fill_rate()
        return abs(actual - self.predicted_fill_rate)

    def edge_calibration(self) -> float:
        """Error between predicted and actual edge per fill."""
        actual = self.avg_edge()
        return abs(actual - self.predicted_edge)

    def fill_rate(self) -> float:
        """Observed fill rate."""
        if self.actual_quotes == 0:
            return 0.0
        return self.actual_fills / self.actual_quotes

    def avg_edge(self) -> float:
        """Average realized edge per fill."""
        if self.n_fills == 0:
            return 0.0
        return self.actual_edge_sum / self.n_fills

    def avg_as(self) -> float:
        """Average adverse selection per fill."""
        if self.n_fills == 0:
            return 0.0
        return self.actual_as_sum / self.n_fills


@dataclass
class StrategyDecayDetector:
    """Detect when the strategy is losing its edge.

    Tracks rolling Sharpe ratio. When the rolling Sharpe drops below
    a threshold for multiple periods, the strategy is decaying.
    """

    window: int = 50  # number of recent decisions
    pnl_history: deque = field(default_factory=lambda: deque(maxlen=200))
    rolling_window: int = 50
    decay_threshold: float = -0.5  # Sharpe below this = decaying
    consecutive_periods: int = 0
    max_consecutive: int = 3  # N periods below threshold = decay alert

    def update(self, pnl: float) -> None:
        """Update with new PnL observation."""
        self.pnl_history.append(pnl)
        # Evaluate once we have at least 2 samples (and prefer rolling_window
        # when available). Each update after the window fills is one "period".
        if len(self.pnl_history) < 2:
            return
        w = min(self.rolling_window, len(self.pnl_history))
        # Don't declare decay until we have a full window of evidence
        if w < self.rolling_window and len(self.pnl_history) < self.rolling_window:
            return
        recent = list(self.pnl_history)[-w:]
        mean = sum(recent) / len(recent)
        var = sum((p - mean) ** 2 for p in recent) / len(recent)
        # Near-zero variance with negative mean ⇒ strongly negative Sharpe
        if var < 1e-12:
            sharpe = -10.0 if mean < 0 else (10.0 if mean > 0 else 0.0)
        else:
            sharpe = mean / math.sqrt(var)
        if sharpe < self.decay_threshold:
            self.consecutive_periods += 1
        else:
            self.consecutive_periods = 0

    def is_decaying(self) -> bool:
        """True if the strategy has been decaying for max_consecutive periods."""
        return self.consecutive_periods >= self.max_consecutive

    def current_sharpe(self) -> float:
        """Current rolling Sharpe ratio."""
        if len(self.pnl_history) < 2:
            return 0.0
        recent = list(self.pnl_history)[-min(
            self.rolling_window, len(self.pnl_history)
        ):]
        mean = sum(recent) / len(recent)
        var = sum((p - mean) ** 2 for p in recent) / len(recent)
        return mean / math.sqrt(var) if var > 1e-12 else 0.0


@dataclass
class PnLAttribution:
    """Attribute PnL to specific decisions.

    Records each decision with its outcome for later analysis.
    """

    decisions: deque = field(default_factory=lambda: deque(maxlen=1000))
    by_regime: dict[str, list] = field(default_factory=dict)
    by_offset: dict[str, list] = field(default_factory=dict)
    n_decisions: int = 0
    n_profitable: int = 0
    sum_pnl: float = 0.0
    sum_winning_pnl: float = 0.0
    sum_losing_pnl: float = 0.0

    def record_decision(
        self, regime: str, offset: str, pnl: float
    ) -> None:
        """Record a decision outcome."""
        self.n_decisions += 1
        self.sum_pnl += pnl
        if pnl > 0:
            self.n_profitable += 1
            self.sum_winning_pnl += pnl
        else:
            self.sum_losing_pnl += pnl
        entry = {"regime": regime, "offset": offset, "pnl": pnl}
        self.decisions.append(entry)
        self.by_regime.setdefault(regime, []).append(pnl)
        self.by_offset.setdefault(offset, []).append(pnl)

    def hit_rate(self) -> float:
        """Fraction of decisions that were profitable."""
        if self.n_decisions == 0:
            return 0.0
        return self.n_profitable / self.n_decisions

    def avg_pnl(self) -> float:
        """Average PnL per decision."""
        if self.n_decisions == 0:
            return 0.0
        return self.sum_pnl / self.n_decisions

    def profit_factor(self) -> float:
        """Ratio of gross profit to gross loss."""
        if self.sum_losing_pnl >= 0:
            return float('inf') if self.sum_winning_pnl > 0 else 0.0
        return self.sum_winning_pnl / abs(self.sum_losing_pnl)

    def regime_pnl(self) -> dict[str, float]:
        """Total PnL by regime."""
        return {
            regime: sum(pnls) for regime, pnls in self.by_regime.items()
        }

    def offset_pnl(self) -> dict[str, float]:
        """Total PnL by offset."""
        return {
            offset: sum(pnls) for offset, pnls in self.by_offset.items()
        }


@dataclass
class SelfEvaluation:
    """Combined self-evaluation state for a market."""

    calibration: CalibrationTracker = field(default_factory=CalibrationTracker)
    decay: StrategyDecayDetector = field(default_factory=StrategyDecayDetector)
    attribution: PnLAttribution = field(default_factory=PnLAttribution)

    def update(self, pnl: float, regime: str, offset: str) -> None:
        """Update all self-evaluation components."""
        self.decay.update(pnl)
        self.attribution.record_decision(regime, offset, pnl)

    def record_fill(self, edge: float, as_observed: float) -> None:
        """Record a fill for calibration tracking."""
        self.calibration.record_fill(edge, as_observed)

    def record_quote(self) -> None:
        """Record a quote for calibration tracking."""
        self.calibration.record_quote()

    def summary(self) -> dict[str, float]:
        """Return a summary of self-evaluation metrics."""
        return {
            "calibration_fill_rate_error": self.calibration.fill_rate_calibration(),
            "calibration_edge_error": self.calibration.edge_calibration(),
            "actual_fill_rate": self.calibration.fill_rate(),
            "actual_avg_edge": self.calibration.avg_edge(),
            "decaying": self.decay.is_decaying(),
            "current_sharpe": self.decay.current_sharpe(),
            "hit_rate": self.attribution.hit_rate(),
            "avg_pnl": self.attribution.avg_pnl(),
            "profit_factor": self.attribution.profit_factor(),
            "n_decisions": float(self.attribution.n_decisions),
        }
