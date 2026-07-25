"""Market regime detector: classify current market conditions.

Before the quoting layer decides WHERE to place orders, the regime
detector decides WHETHER to quote at all and HOW aggressively.

Regimes:
  - QUIET: tight book, low flow, rewards are the main income
  - TRENDING: persistent one-sided flow, inventory risk is high
  - VOLATILE: large price swings, widen spread and reduce size
  - TOXIC: post-fill markout is bad, widen spread aggressively
  - STALE: no recent data, skip quoting (avoid stale prices)
  - DEAD: no volume, skip quoting (low reward, high adverse selection)

This is computed from a feature vector extracted from the order book
and recent trade flow. No external API, no LLM — pure statistics on
the live data feed.

Pure functions only — no I/O. The engine calls detect_regime() on
each requote with the latest features.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class MarketRegime(Enum):
    """Trading regime for a single market."""

    QUIET = "QUIET"
    TRENDING = "TRENDING"
    VOLATILE = "VOLATILE"
    TOXIC = "TOXIC"
    STALE = "STALE"
    DEAD = "DEAD"


@dataclass
class MarketFeatures:
    """Feature vector extracted from order book + recent flow.

    These are the inputs to the regime detector. All values are computed
    from the live data feed (no external API).
    """

    # Spread health
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread_ticks: float = 0.0  # (ask - bid) / tick
    mid_price: float = 0.0

    # Depth
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    depth_imbalance: float = 0.0  # (bid - ask) / (bid + ask), [-1, +1]

    # Flow
    flow_z: float = 0.0  # signed flow z-score (from FlowEstimator)
    flow_window: int = 100  # number of recent trades

    # Volatility
    vol_short: float = 0.0  # per-second vol (from VolEstimator)
    vol_long: float = 0.0
    vol_ratio: float = 0.0  # vol_short / vol_long

    # Toxicity (post-fill markout)
    markout_short: float = 0.0  # 30s markout
    markout_medium: float = 0.0  # 120s
    toxicity: float = 0.0  # from MarkoutTracker

    # Time since last update
    seconds_since_last_update: float = 0.0
    n_trades_last_hour: int = 0

    # Reward context
    reward_band_cents: float = 0.0  # reward band in cents
    rewards_daily_rate: float = 0.0  # $/day pool

    @property
    def is_dead(self) -> bool:
        """Market is dead: no recent activity, no rewards."""
        return self.n_trades_last_hour == 0 and self.rewards_daily_rate == 0.0

    @property
    def is_stale(self) -> bool:
        """Market data is stale: no recent updates."""
        return self.seconds_since_last_update > 60.0


@dataclass
class RegimeDecision:
    """Output of the regime detector.

    Tells the engine:
    - regime: which regime we're in
    - should_quote: whether to place orders at all
    - spread_multiplier: how much to widen the spread (1.0 = normal, 2.0 = double)
    - size_multiplier: how much to reduce the size (1.0 = normal, 0.5 = half)
    - skip_reason: why we're not quoting (if should_quote is False)
    """

    regime: MarketRegime
    should_quote: bool
    spread_multiplier: float = 1.0
    size_multiplier: float = 1.0
    skip_reason: str = ""


def detect_regime(features: MarketFeatures) -> RegimeDecision:
    """Classify the current market regime and decide whether to quote.

    Decision tree:
    1. If dead or stale → skip
    2. If toxic (markout < -0.01) → widen spread 2x, half size
    3. If volatile (vol_ratio > 5) → widen spread 1.5x, half size
    4. If trending (|flow_z| > 2) → widen spread 1.5x, full size
    5. Otherwise → quiet, full size, normal spread

    Thresholds are conservative defaults; the engine can override them
    via the profile parameters.
    """
    # 1. Dead or stale
    if features.is_dead:
        return RegimeDecision(
            regime=MarketRegime.DEAD,
            should_quote=False,
            skip_reason="dead_market",
        )
    if features.is_stale:
        return RegimeDecision(
            regime=MarketRegime.STALE,
            should_quote=False,
            skip_reason="stale_data",
        )

    # 2. Toxic: post-fill markout is bad
    if features.toxicity > 0.01:
        return RegimeDecision(
            regime=MarketRegime.TOXIC,
            should_quote=True,
            spread_multiplier=2.0,
            size_multiplier=0.5,
        )

    # 3. Volatile: short-term vol is much higher than long-term
    if features.vol_ratio > 5.0:
        return RegimeDecision(
            regime=MarketRegime.VOLATILE,
            should_quote=True,
            spread_multiplier=1.5,
            size_multiplier=0.5,
        )

    # 4. Trending: persistent one-sided flow
    if abs(features.flow_z) > 2.0:
        return RegimeDecision(
            regime=MarketRegime.TRENDING,
            should_quote=True,
            spread_multiplier=1.5,
            size_multiplier=1.0,
        )

    # 5. Quiet: default
    return RegimeDecision(
        regime=MarketRegime.QUIET,
        should_quote=True,
        spread_multiplier=1.0,
        size_multiplier=1.0,
    )


@dataclass
class FeatureExtractor:
    """Extract MarketFeatures from raw order book + trade data.

    Maintains a rolling window of recent trades for flow/volume stats.
    Pure state machine — no I/O. The engine feeds it book snapshots and
    trade prints via update_book() and update_trade(), and queries via
    extract().
    """

    recent_trades: deque = field(default_factory=lambda: deque(maxlen=200))
    last_book_update_ts: float = 0.0
    last_trade_ts: float = 0.0
    # For depth imbalance tracking
    last_bid_depth: float = 0.0
    last_ask_depth: float = 0.0

    def update_book(
        self, best_bid: float, best_ask: float, bid_depth: float,
        ask_depth: float, ts: float,
    ) -> None:
        """Record a book snapshot."""
        self.last_book_update_ts = ts
        self.last_bid_depth = bid_depth
        self.last_ask_depth = ask_depth
        # Mid price
        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2.0
            # Update trade book context for future extractions
            self._last_mid = mid
            self._last_bid = best_bid
            self._last_ask = best_ask

    def update_trade(
        self, side: str, price: float, size: float, ts: float,
    ) -> None:
        """Record a trade print (for flow / volume stats)."""
        self.recent_trades.append({
            "side": side,
            "price": price,
            "size": size,
            "ts": ts,
        })
        self.last_trade_ts = ts

    def extract(
        self, now: float, vol_short: float = 0.0, vol_long: float = 0.0,
        flow_z: float = 0.0, markout_short: float = 0.0,
        markout_medium: float = 0.0, toxicity: float = 0.0,
        tick_size: float = 0.001, reward_band_cents: float = 0.0,
        rewards_daily_rate: float = 0.0,
    ) -> MarketFeatures:
        """Extract a MarketFeatures snapshot at time `now`."""
        # Get current book state from last update
        bid = getattr(self, "_last_bid", 0.0)
        ask = getattr(self, "_last_ask", 0.0)
        mid = getattr(self, "_last_mid", 0.0)

        spread_ticks = 0.0
        if bid > 0 and ask > 0 and tick_size > 0:
            spread_ticks = (ask - bid) / tick_size

        total_depth = self.last_bid_depth + self.last_ask_depth
        depth_imbalance = 0.0
        if total_depth > 0:
            depth_imbalance = (self.last_bid_depth - self.last_ask_depth) / total_depth

        # Count trades in the last hour
        one_hour_ago = now - 3600.0
        n_trades_last_hour = sum(
            1 for t in self.recent_trades if t["ts"] > one_hour_ago
        )

        # Vol ratio (avoid div by zero)
        vol_ratio = vol_short / vol_long if vol_long > 1e-9 else 1.0

        # Time since last update
        seconds_since_last_update = 0.0
        if self.last_book_update_ts > 0:
            seconds_since_last_update = now - self.last_book_update_ts

        return MarketFeatures(
            best_bid=bid,
            best_ask=ask,
            spread_ticks=spread_ticks,
            mid_price=mid,
            bid_depth=self.last_bid_depth,
            ask_depth=self.last_ask_depth,
            depth_imbalance=depth_imbalance,
            flow_z=flow_z,
            flow_window=len(self.recent_trades),
            vol_short=vol_short,
            vol_long=vol_long,
            vol_ratio=vol_ratio,
            markout_short=markout_short,
            markout_medium=markout_medium,
            toxicity=toxicity,
            seconds_since_last_update=seconds_since_last_update,
            n_trades_last_hour=n_trades_last_hour,
            reward_band_cents=reward_band_cents,
            rewards_daily_rate=rewards_daily_rate,
        )
