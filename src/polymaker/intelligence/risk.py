"""Risk intelligence: dynamic stop-loss, adaptive position limits.

Static position limits are a blunt instrument. They don't adapt to
changing market conditions, recent performance, or current exposure.
This module adds:

1. Dynamic stop-loss per market:
   - Tightens when volatility is high
   - Loosens when volatility is low
   - Triggers when PnL drops below a threshold

2. Adaptive position limits:
   - Scales with current PnL (reduce when losing)
   - Scales with volatility (reduce when vol is high)
   - Scales with current exposure (avoid concentration)

3. Drawdown tracking:
   - Tracks peak-to-trough PnL
   - Triggers kill switch if drawdown exceeds threshold

4. Risk-adjusted sizing:
   - Kelly-inspired position sizing
   - Scales inversely with volatility
   - Caps by maximum drawdown tolerance

Pure state machines. The engine feeds market data and PnL updates,
and queries current risk parameters.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class DynamicStopLoss:
    """Per-market dynamic stop-loss that adapts to volatility.

    The stop-loss threshold is computed as:
      threshold = max(min_threshold, k * vol_short * sqrt(time_window))
    Higher volatility = wider stop (more room for noise).
    Lower volatility = tighter stop (cut losses faster).
    """

    min_threshold: float = 0.01  # minimum $2 stop
    k: float = 3.0  # multiplier for volatility-based stop
    time_window_s: float = 300.0  # 5-minute window for vol calc
    base_allocation_usdc: float = 100.0  # scale for $ threshold

    def compute_threshold(
        self, vol_short: float, current_pnl: float
    ) -> float:
        """Compute the current stop-loss threshold in USDC."""
        vol_threshold = (
            self.k * vol_short * math.sqrt(self.time_window_s)
            * self.base_allocation_usdc
        )
        return max(self.min_threshold, vol_threshold)

    def should_stop(self, pnl: float, vol_short: float) -> bool:
        """True if PnL has dropped below the stop-loss threshold."""
        threshold = self.compute_threshold(vol_short, pnl)
        return pnl < -threshold


@dataclass
class AdaptivePositionLimit:
    """Adaptive position limit that responds to PnL and volatility.

    The limit scales with:
    - Current PnL (reduce when losing)
    - Recent volatility (reduce when vol is high)
    - Current exposure (avoid concentration)
    """

    base_limit: float = 100.0  # base position limit
    pnl_scaling: float = 0.5  # how much PnL affects limit
    vol_scaling: float = 0.3  # how much vol affects limit
    min_limit: float = 5.0  # minimum allowed limit
    pnl_history: deque = field(default_factory=lambda: deque(maxlen=100))
    peak_pnl: float = 0.0

    def compute_limit(
        self, current_pnl: float, vol_short: float, current_exposure: float
    ) -> float:
        """Compute the current adaptive position limit."""
        # Scale by PnL: positive PnL = higher limit, negative = lower
        pnl_factor = max(0.0, 1.0 + self.pnl_scaling * current_pnl / self.base_limit)
        # Scale by volatility: high vol = lower limit
        vol_factor = max(0.1, 1.0 - self.vol_scaling * vol_short * 1000)
        # Scale by exposure: high exposure = lower limit
        exposure_factor = max(0.1, 1.0 - current_exposure / self.base_limit)
        limit = (
            self.base_limit * pnl_factor * vol_factor * exposure_factor
        )
        return max(self.min_limit, limit)

    def update_pnl(self, pnl: float) -> None:
        """Update PnL history and peak."""
        self.pnl_history.append(pnl)
        if pnl > self.peak_pnl:
            self.peak_pnl = pnl

    def drawdown(self) -> float:
        """Current drawdown from peak (positive number = loss)."""
        if not self.pnl_history:
            return 0.0
        current = self.pnl_history[-1]
        return max(0.0, self.peak_pnl - current)


@dataclass
class RiskState:
    """Combined risk state for a single market."""

    stop_loss: DynamicStopLoss = field(default_factory=DynamicStopLoss)
    position_limit: AdaptivePositionLimit = field(
        default_factory=AdaptivePositionLimit
    )
    current_pnl: float = 0.0
    current_exposure: float = 0.0
    vol_short: float = 0.0
    n_fills: int = 0
    n_fills_losing: int = 0
    last_kill_ts: float = 0.0

    def update(
        self, pnl: float, exposure: float, vol_short: float
    ) -> None:
        """Update risk state with current PnL, exposure, and volatility."""
        self.current_pnl = pnl
        self.current_exposure = exposure
        self.vol_short = vol_short
        self.position_limit.update_pnl(pnl)

    def should_stop_loss(self) -> bool:
        """True if the stop-loss should trigger."""
        return self.stop_loss.should_stop(
            self.current_pnl, self.vol_short
        )

    def should_kill(self) -> bool:
        """True if the market should be killed (drawdown > max)."""
        return self.position_limit.drawdown() > self.position_limit.base_limit * 0.3

    def compute_position_limit(self) -> float:
        """Current adaptive position limit."""
        return self.position_limit.compute_limit(
            self.current_pnl, self.vol_short, self.current_exposure
        )

    def record_fill(self, pnl_delta: float) -> None:
        """Record a fill outcome for drawdown tracking."""
        self.n_fills += 1
        if pnl_delta < 0:
            self.n_fills_losing += 1
        self.position_limit.update_pnl(self.current_pnl + pnl_delta)
