"""Fill calibration table schema (fixture-first when live data is thin).

Predicted fill probability bins vs observed fill rates. Empty live sample
is valid — schema + merge path must still work for tiny-live later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Default bins matching the roadmap table
DEFAULT_BINS: list[tuple[float, float, float]] = [
    # (lo, hi, target_actual_rate)
    (0.0, 0.10, 0.05),
    (0.10, 0.30, 0.20),
    (0.30, 0.60, 0.45),
    (0.60, 1.01, 0.75),
]


@dataclass
class CalibrationBin:
    pred_lo: float
    pred_hi: float
    target_rate: float
    n_predicted: int = 0
    n_filled: int = 0

    @property
    def actual_rate(self) -> float:
        if self.n_predicted <= 0:
            return 0.0
        return self.n_filled / self.n_predicted

    @property
    def error(self) -> float:
        return self.actual_rate - self.target_rate

    def as_dict(self) -> dict[str, Any]:
        return {
            "pred_lo": self.pred_lo,
            "pred_hi": self.pred_hi,
            "target_rate": self.target_rate,
            "n_predicted": self.n_predicted,
            "n_filled": self.n_filled,
            "actual_rate": round(self.actual_rate, 4),
            "error": round(self.error, 4),
        }


@dataclass
class FillCalibrationTable:
    """Predicted vs actual fill rates by probability bin."""

    bins: list[CalibrationBin] = field(default_factory=list)
    n_live_samples: int = 0
    source: str = "fixture"  # fixture | tiny_live | empty

    @classmethod
    def default_empty(cls) -> FillCalibrationTable:
        return cls(
            bins=[
                CalibrationBin(lo, hi, tgt) for lo, hi, tgt in DEFAULT_BINS
            ],
            n_live_samples=0,
            source="empty",
        )

    def record(self, predicted_p: float, filled: bool) -> None:
        p = max(0.0, min(1.0, float(predicted_p)))
        for b in self.bins:
            if b.pred_lo <= p < b.pred_hi or (b.pred_hi >= 1.0 and p >= b.pred_lo):
                b.n_predicted += 1
                if filled:
                    b.n_filled += 1
                self.n_live_samples += 1
                if self.source == "empty":
                    self.source = "tiny_live"
                return

    def max_abs_error(self) -> float:
        errs = [abs(b.error) for b in self.bins if b.n_predicted >= 5]
        return max(errs) if errs else 0.0

    def is_calibrated(self, *, max_err: float = 0.25, min_samples: int = 20) -> bool:
        if self.n_live_samples < min_samples:
            return False
        return self.max_abs_error() <= max_err

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "n_live_samples": self.n_live_samples,
            "max_abs_error": round(self.max_abs_error(), 4),
            "calibrated": self.is_calibrated(),
            "bins": [b.as_dict() for b in self.bins],
        }
