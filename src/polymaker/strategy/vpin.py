"""VPIN: Volume-Synchronized Probability of Informed Trading.

Reference: Easley, Lopez de Prado, O'Hara (2012), "Flow Toxicity and Liquidity
in a High Frequency World"

VPIN measures the fraction of volume driven by informed traders by bucketing
trade volume into constant-volume buckets V_bucket and measuring absolute buy/sell
imbalance across N consecutive buckets:

    VPIN = sum(|V_tau^B - V_tau^S|) / (N * V_bucket)

Where:
  V_tau^B: buy volume in bucket tau
  V_tau^S: sell volume in bucket tau
  N: number of buckets in rolling window (e.g. 50)
  V_bucket: fixed volume per bucket (e.g. 100 shares or $50 notional)

VPIN ranges in [0, 1]. Elevated VPIN (e.g. > 0.4) indicates high probability
of informed order flow and adverse selection risk. The quoter uses VPIN to:
1. Widen the economic half-spread delta
2. Reduce entry order sizing
3. Trigger defensive regime posture (EVENT / REDUCE_ONLY)

Pure state machine — no I/O.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from polymaker.domain import Side


@dataclass
class VPINBucket:
    buy_vol: float = 0.0
    sell_vol: float = 0.0

    @property
    def total_vol(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def imbalance(self) -> float:
        return abs(self.buy_vol - self.sell_vol)


class VPINEstimator:
    """Volume-Synchronized Probability of Informed Trading estimator."""

    def __init__(self, bucket_volume: float = 100.0, n_buckets: int = 20) -> None:
        if bucket_volume <= 0:
            raise ValueError("bucket_volume must be positive")
        if n_buckets <= 0:
            raise ValueError("n_buckets must be positive")

        self.bucket_volume = bucket_volume
        self.n_buckets = n_buckets

        self._completed_buckets: deque[VPINBucket] = deque(maxlen=n_buckets)
        self._current_bucket = VPINBucket()

    def update(self, aggressor: Side, size: float) -> float:
        """Process a trade print of size shares.
        
        Fills volume buckets in sequence and returns current VPIN [0, 1].
        """
        if size <= 0:
            return self.vpin

        rem = size
        is_buy = aggressor is Side.BUY

        while rem > 0:
            cur_fill = self._current_bucket.total_vol
            needed = self.bucket_volume - cur_fill

            if rem >= needed:
                # Fill current bucket completely
                if is_buy:
                    self._current_bucket.buy_vol += needed
                else:
                    self._current_bucket.sell_vol += needed

                self._completed_buckets.append(self._current_bucket)
                self._current_bucket = VPINBucket()
                rem -= needed
            else:
                # Partial fill of current bucket
                if is_buy:
                    self._current_bucket.buy_vol += rem
                else:
                    self._current_bucket.sell_vol += rem
                rem = 0.0

        return self.vpin

    @property
    def vpin(self) -> float:
        """Compute current VPIN over completed buckets."""
        if not self._completed_buckets:
            return 0.0

        total_imbalance = sum(b.imbalance for b in self._completed_buckets)
        total_volume = sum(b.total_vol for b in self._completed_buckets)

        if total_volume <= 0:
            return 0.0

        return total_imbalance / total_volume

    @property
    def is_ready(self) -> bool:
        """True when at least n_buckets/2 buckets have been filled."""
        return len(self._completed_buckets) >= max(1, self.n_buckets // 2)

    def reset(self) -> None:
        self._completed_buckets.clear()
        self._current_bucket = VPINBucket()
