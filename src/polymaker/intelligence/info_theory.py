"""Information-theoretic features: entropy, KL divergence, mutual information.

Classical market-making features (spread, flow, depth) are necessary
but miss higher-order patterns. Information theory gives us tools
to detect:

1. Shannon entropy of price changes:
   - High entropy = noisy, unpredictable (regime change)
   - Low entropy = predictable trend or consolidation

2. KL divergence between expected and actual price distribution:
   - When KL divergence is high, the market is behaving differently
   from our prior beliefs → regime change, adjust parameters

3. Conditional entropy of next price given current state:
   - Lower conditional entropy = market is more predictable
   - Higher conditional entropy = more random

4. Autocorrelation of price changes:
   - Positive autocorrelation = momentum
   - Negative autocorrelation = mean reversion
   - Zero = random walk

5. Transfer entropy between order flow and price:
   - High TE = order flow is informative about future price
   - Asymmetric TE in BUY vs SELL flow = directional signal

Pure functions only — no I/O. The engine feeds observations via
update() and queries computed features via extract().
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class EntropyTracker:
    """Track Shannon entropy of price changes.

    Discretizes price changes into bins and computes entropy.
    High entropy = unpredictable; low entropy = predictable.
    """

    bin_edges: list = field(default_factory=lambda: [
        -0.05, -0.02, -0.01, -0.005, -0.002, -0.001, 0,
        0.001, 0.002, 0.005, 0.01, 0.02, 0.05
    ])
    bin_counts: list = field(default_factory=lambda: [0] * 13)
    n_obs: int = 0
    last_mid: float = 0.0

    def update(self, mid: float) -> None:
        """Update with new mid-price."""
        if self.last_mid > 0:
            change = mid - self.last_mid
            self._bin(change)
        self.last_mid = mid
        self.n_obs += 1

    def _bin(self, change: float) -> None:
        """Bin a change value into the histogram."""
        for i, edge in enumerate(self.bin_edges):
            if change <= edge:
                self.bin_counts[i] += 1
                return
        self.bin_counts[-1] += 1  # overflow bin

    def entropy(self) -> float:
        """Compute Shannon entropy in nats.

        H = -sum(p_i * ln(p_i))
        Higher = more uncertain.
        """
        if self.n_obs == 0:
            return 0.0
        total = sum(self.bin_counts)
        if total == 0:
            return 0.0
        h = 0.0
        for c in self.bin_counts:
            if c > 0:
                p = c / total
                h -= p * math.log(p)
        return h

    def normalized_entropy(self) -> float:
        """Entropy normalized to [0, 1] by max possible entropy.

        H / log(N) where N is number of non-empty bins.
        1.0 = max uncertainty, 0.0 = deterministic.
        """
        n_bins = sum(1 for c in self.bin_counts if c > 0)
        if n_bins <= 1:
            return 0.0
        max_h = math.log(n_bins)
        return self.entropy() / max_h if max_h > 0 else 0.0


@dataclass
class KLDivergenceTracker:
    """Track KL divergence between two distributions.

    KL(P || Q) = sum(P(x) * log(P(x) / Q(x)))
    Measures how one distribution differs from another.
    Used to detect: when does actual market behavior differ from
    our prior expectations?
    """

    reference_counts: list = field(default_factory=list)
    actual_counts: list = field(default_factory=list)
    n_obs: int = 0

    def update(self, value: float, is_reference: bool = True, bin_size: float = 0.001) -> None:
        """Update with a new value, binning into `bin_size` buckets."""
        bin_idx = int(value / bin_size)
        target = self.reference_counts if is_reference else self.actual_counts
        while len(target) <= bin_idx:
            target.append(0)
        target[bin_idx] += 1
        if not is_reference:
            self.n_obs += 1

    def divergence(self) -> float:
        """Compute KL(reference || actual)."""
        if not self.reference_counts or not self.actual_counts:
            return 0.0
        max_len = max(len(self.reference_counts), len(self.actual_counts))
        ref_total = sum(self.reference_counts) or 1
        act_total = sum(self.actual_counts) or 1
        kl = 0.0
        for i in range(max_len):
            r = self.reference_counts[i] if i < len(self.reference_counts) else 0
            a = self.actual_counts[i] if i < len(self.actual_counts) else 0
            if r > 0 and a > 0:
                p = r / ref_total
                q = a / act_total
                kl += p * math.log(p / q)
        return kl


@dataclass
class AutocorrelationTracker:
    """Track autocorrelation of price changes at multiple lags.

    Positive autocorrelation = momentum (trend continues)
    Negative autocorrelation = mean reversion (trend reverses)
    Zero = random walk
    """

    n_lags: int = 5
    returns: deque = field(default_factory=lambda: deque(maxlen=200))
    n_obs: int = 0
    _last_mid: float = 0.0

    def update(self, mid: float) -> None:
        """Update with new mid-price."""
        if self.n_obs > 0 and self._last_mid > 0:
            ret = mid - self._last_mid
            self.returns.append(ret)
        self._last_mid = mid
        self.n_obs += 1

    def autocorrelations(self) -> list[float]:
        """Compute autocorrelation at lags 1..n_lags."""
        if len(self.returns) < self.n_lags + 1:
            return [0.0] * self.n_lags
        # Use the returns directly
        rets = list(self.returns)
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / n
        if var < 1e-12:
            # Constant non-zero returns ⇒ perfect lag-k correlation (momentum
            # of a pure trend). Zero returns ⇒ undefined, report 0.
            if abs(mean) > 1e-15:
                return [1.0] * self.n_lags
            return [0.0] * self.n_lags
        result = []
        for lag in range(1, self.n_lags + 1):
            cov = sum(
                (rets[i] - mean) * (rets[i - lag] - mean)
                for i in range(lag, n)
            ) / n
            result.append(cov / var)
        return result


@dataclass
class TransferEntropyTracker:
    """Track transfer entropy between order flow and price.

    TE(X -> Y) = H(Y_t | Y_{t-1}) - H(Y_t | Y_{t-1}, X_{t-1})
    Measures how much knowing X reduces uncertainty about Y.
    If TE is high, X is informative about Y.
    """

    n_lags: int = 2
    price_returns: deque = field(default_factory=lambda: deque(maxlen=100))
    flow_returns: deque = field(default_factory=lambda: deque(maxlen=100))
    n_obs: int = 0
    last_mid: float = 0.0
    last_flow: float = 0.0

    def update(self, mid: float, flow: float) -> None:
        """Update with new mid-price and flow signal."""
        if self.n_obs > 0:
            self.price_returns.append(mid - self.last_mid)
            self.flow_returns.append(flow - self.last_flow)
        self.last_mid = mid
        self.last_flow = flow
        self.n_obs += 1

    def transfer_entropy(self) -> float:
        """Estimate transfer entropy from flow to price.

        Simplified: TE_flow→price = correlation(flow_lag, price_change)
        Higher = flow is more informative about future price.
        """
        if len(self.price_returns) < self.n_lags + 1:
            return 0.0
        if len(self.flow_returns) < self.n_lags + 1:
            return 0.0
        # Use lag-1 correlation as a proxy for TE
        prices = list(self.price_returns)
        flows = list(self.flow_returns)
        n = min(len(prices), len(flows))
        mean_p = sum(prices) / n
        mean_f = sum(flows) / n
        var_p = sum((p - mean_p) ** 2 for p in prices) / n
        var_f = sum((f - mean_f) ** 2 for f in flows) / n
        if var_p < 1e-12 or var_f < 1e-12:
            return 0.0
        cov = sum(
            (flows[i] - mean_f) * (prices[i] - mean_p)
            for i in range(n)
        ) / n
        return cov / math.sqrt(var_p * var_f)


@dataclass
class InformationFeatures:
    """Container for all information-theoretic features."""

    entropy_nats: float = 0.0
    normalized_entropy: float = 0.0
    kl_divergence: float = 0.0
    autocorrelations: list = field(default_factory=list)
    transfer_entropy: float = 0.0
    n_observations: int = 0


@dataclass
class InformationProcessor:
    """Track all information-theoretic features for a market.

    Pure state machine. The engine feeds observations via update()
    and queries computed features via extract().
    """

    entropy: EntropyTracker = field(default_factory=EntropyTracker)
    kl: KLDivergenceTracker = field(default_factory=KLDivergenceTracker)
    autocorr: AutocorrelationTracker = field(default_factory=AutocorrelationTracker)
    transfer: TransferEntropyTracker = field(
        default_factory=TransferEntropyTracker
    )
    n_updates: int = 0

    def update(self, mid: float, flow: float = 0.0) -> None:
        """Update with new mid-price and (optionally) flow signal."""
        self.n_updates += 1
        self.entropy.update(mid)
        self.kl.update(mid, is_reference=(self.n_updates % 2 == 0))
        self.kl.update(mid, is_reference=False)
        self.autocorr.update(mid)
        self.transfer.update(mid, flow)

    def extract(self) -> InformationFeatures:
        """Extract all information-theoretic features."""
        return InformationFeatures(
            entropy_nats=self.entropy.entropy(),
            normalized_entropy=self.entropy.normalized_entropy(),
            kl_divergence=self.kl.divergence(),
            autocorrelations=self.autocorr.autocorrelations(),
            transfer_entropy=self.transfer.transfer_entropy(),
            n_observations=self.n_updates,
        )
