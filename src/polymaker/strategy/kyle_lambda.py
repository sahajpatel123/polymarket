"""Kyle's Lambda and Glosten-Milgrom Adverse Selection Model.

References:
- Kyle (1985), "Continuous Auctions and Informed Trader"
- Glosten & Milgrom (1985), "Bid, ask and transaction prices in a specialist market"

Kyle's Lambda (lambda = dP / dV) measures the price impact per unit of net order
flow. In liquid markets, lambda is small (trade flow moves price very little). In
illiquid or informed markets, lambda is large (small trades move price significantly).

Formulas:
    delta_mid = mid_t - mid_{t-1}
    signed_vol = aggressor_side * trade_size
    lambda_t = EWMA(delta_mid / signed_vol)

    Adverse Selection Spread Component = 2 * lambda * order_size

Pure state machine — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.domain import Side


@dataclass
class KyleLambdaEstimator:
    """Estimates price impact parameter lambda = dP / dV."""

    halflife_s: float = 300.0
    _lambda: float = 0.0001  # price impact per share (in price units / share)
    _last_mid: float | None = None
    _last_ts: float = 0.0
    _initialized: bool = False

    def update(self, mid: float, aggressor: Side, size: float, ts: float) -> float:
        """Update lambda given a trade print and contemporaneous mid price change.
        
        Returns updated lambda in price_units per share.
        """
        if size <= 0 or mid <= 0:
            return self._lambda

        if self._last_mid is not None:
            dt = max(0.0, ts - self._last_ts)
            d_mid = mid - self._last_mid
            signed_vol = size if aggressor is Side.BUY else -size

            if abs(signed_vol) > 1e-6:
                # Obs price impact = |d_mid| / volume
                obs_lambda = abs(d_mid) / abs(signed_vol)
                # Cap outlier spikes (e.g. 10 ticks per share)
                obs_lambda = min(0.05, obs_lambda)

                if not self._initialized:
                    self._lambda = obs_lambda
                    self._initialized = True
                else:
                    decay = 0.5 ** (dt / self.halflife_s) if self.halflife_s > 0 else 0.5
                    self._lambda = decay * self._lambda + (1.0 - decay) * obs_lambda

        self._last_mid = mid
        self._last_ts = ts
        return self._lambda

    @property
    def lambda_param(self) -> float:
        """Current estimated Kyle's lambda (price change per share)."""
        return self._lambda

    def adverse_selection_spread(self, size: float) -> float:
        """Expected adverse selection cost for an order of size shares.
        
        Adverse selection = 2 * lambda * size
        """
        return 2.0 * self._lambda * size
