"""GARCH(1,1) volatility estimator for principled spread sizing.

EWMA vol (RiskMetrics) is a special case of IGARCH. Full GARCH(1,1)
lets persistence (β) and shock sensitivity (α) differ, which matters
when clustering of large moves should widen quotes more than a single
EWMA half-life allows.

    σ²_t = ω + α · r²_{t-1} + β · σ²_{t-1}

Constraints for stationarity / positivity:
    ω > 0, α ≥ 0, β ≥ 0, α + β < 1

Pure state machine — feed returns, read σ. No I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class GARCHVolatility:
    """Online GARCH(1,1) on a scalar return series."""

    omega: float = 1e-8
    alpha: float = 0.05
    beta: float = 0.90
    _var: float = 0.0
    _initialized: bool = False
    _n: int = 0

    def __post_init__(self) -> None:
        if self.omega <= 0:
            raise ValueError("omega must be positive")
        if self.alpha < 0 or self.beta < 0:
            raise ValueError("alpha and beta must be non-negative")
        if self.alpha + self.beta >= 1.0:
            raise ValueError("alpha + beta must be < 1 for stationarity")

    def update(self, ret: float) -> float:
        """Update with a new return; return current σ (not variance)."""
        r2 = float(ret) * float(ret)
        if not self._initialized:
            # Seed variance with first squared return (floor at omega).
            self._var = max(self.omega, r2)
            self._initialized = True
            self._n = 1
            return math.sqrt(self._var)

        self._var = self.omega + self.alpha * r2 + self.beta * self._var
        self._var = max(self.omega, self._var)
        self._n += 1
        return math.sqrt(self._var)

    @property
    def variance(self) -> float:
        return self._var

    @property
    def sigma(self) -> float:
        return math.sqrt(max(0.0, self._var))

    @property
    def ready(self) -> bool:
        return self._initialized

    @property
    def n_updates(self) -> int:
        return self._n

    def unconditional_variance(self) -> float:
        """Long-run variance ω / (1 - α - β)."""
        denom = 1.0 - self.alpha - self.beta
        return self.omega / denom if denom > 1e-12 else self.omega
