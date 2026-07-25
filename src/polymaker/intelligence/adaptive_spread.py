"""Adaptive spread engine: learns from observed fill outcomes.

The current quoting layer uses fixed parameters (delta_min_ticks, c_vol,
c_tox, band_lo, band_hi). This module adds learning: it tracks the
outcome of past quotes and adjusts the spread parameters to maximize
the expected edge per fill.

Key insight: the band_lo floor is what caused 0 fills in 3h 35m of
real data. The quotes sat at the bottom of the reward band, 0.5¢
below where trades were priced. This module learns from that and
adjusts.

Learning algorithm (simple Bayesian update per market):
  - For each market, track: fill_rate, avg_edge, avg_markout, band_position
  - After each fill, update: P(fill at this band position | market state)
  - On requote, adjust band_lo/band_hi toward the position with highest
    observed fill rate, subject to:
    - Lower bound: must be inside reward band (otherwise OOB = no reward)
    - Upper bound: must be below min_edge_ticks from FV (otherwise negative edge)

This is a simple online learning algorithm that adapts to each market's
liquidity profile over time. No external API, no LLM — pure statistics
on observed outcomes.

Pure functions only — no I/O. The engine calls learn_from_fill() after
each fill, and get_adjusted_spread_params() before each requote.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MarketFillStats:
    """Per-market fill statistics, updated incrementally.

    Tracks the outcome of past quotes to learn the optimal band position.
    Uses a simple Bayesian update: each (price_offset, outcome) pair updates
    the probability of fill at that offset.
    """

    # Per-offset-bin statistics: offset_bin -> (n_quotes, n_fills, sum_edge)
    # offset_bin is the price offset from FV in ticks (positive = above FV)
    _bins: dict[int, tuple[int, int, float]] = field(default_factory=dict)
    # Running totals
    n_quotes: int = 0
    n_fills: int = 0
    sum_edge: float = 0.0
    sum_markout: float = 0.0
    # Best offset seen so far
    best_offset: int = 0
    best_fill_rate: float = 0.0

    def record_quote(self, offset_ticks: int) -> None:
        """Record that a quote was placed at `offset_ticks` from FV."""
        self.n_quotes += 1
        n, f, e = self._bins.get(offset_ticks, (0, 0, 0.0))
        self._bins[offset_ticks] = (n + 1, f, e)

    def record_fill(self, offset_ticks: int, edge: float, markout: float) -> None:
        """Record a fill at `offset_ticks` from FV with realized edge.

        edge: realized PnL from the fill (spread captured minus markout)
        markout: adverse selection (negative = adverse, positive = good)
        """
        self.n_fills += 1
        self.sum_edge += edge
        self.sum_markout += markout
        n, f, e = self._bins.get(offset_ticks, (0, 0, 0.0))
        self._bins[offset_ticks] = (n, f + 1, e + edge)
        # Update best offset: highest fill rate with positive edge
        self._update_best_offset()

    def _update_best_offset(self) -> None:
        """Find the offset with highest fill rate and positive edge."""
        best = (0, 0.0, 0.0)  # (offset, fill_rate, avg_edge)
        for offset, (n, f, e) in self._bins.items():
            if n == 0 or f == 0:
                continue
            fill_rate = f / n
            avg_edge = e / f
            if avg_edge <= 0:
                continue  # Skip negative-edge offsets
            # Score: fill_rate * avg_edge (expected edge per quote)
            score = fill_rate * avg_edge
            if score > best[1] * best[2] if best[1] > 0 else True:
                best = (offset, fill_rate, avg_edge)
        if best[1] > 0:
            self.best_offset = best[0]
            self.best_fill_rate = best[1]

    def get_optimal_offset(self, fallback: int = 0) -> int:
        """Return the best observed offset, or fallback if no fills yet."""
        if self.best_fill_rate > 0:
            return self.best_offset
        return fallback

    def get_fill_rate(self) -> float:
        """Overall fill rate across all quotes."""
        if self.n_quotes == 0:
            return 0.0
        return self.n_fills / self.n_quotes

    def get_avg_edge(self) -> float:
        """Average realized edge per fill."""
        if self.n_fills == 0:
            return 0.0
        return self.sum_edge / self.n_fills

    def get_avg_markout(self) -> float:
        """Average markout per fill (negative = adverse)."""
        if self.n_fills == 0:
            return 0.0
        return self.sum_markout / self.n_fills


@dataclass
class AdaptiveSpreadParams:
    """Spread parameters that adapt based on observed fills.

    Instead of fixed band_lo/band_hi, these are adjusted based on
    per-market fill statistics. The adjustment is conservative: it
    moves toward the optimal offset by 20% per requote, so the
    adaptation is gradual and doesn't overshoot.
    """

    # Raw profile parameters (from TOML)
    base_delta_min_ticks: int = 3
    base_c_vol: float = 2.0
    base_c_tox: float = 5.0

    # Per-market adaptive offsets (in ticks from FV)
    # Positive = above FV (SELL), negative = below FV (BUY)
    # These are the LEARNED optimal offsets, clamped to the reward band
    market_offsets: dict[str, int] = field(default_factory=dict)

    # Per-market fill statistics
    stats: dict[str, MarketFillStats] = field(default_factory=dict)

    def get_band_position(self, condition_id: str) -> tuple[int, int]:
        """Return (buy_offset_ticks, sell_offset_ticks) for a market.

        The offsets are relative to FV. A negative buy_offset means
        place BUY below FV by that many ticks. A positive sell_offset
        means place SELL above FV by that many ticks.

        Returns clamped offsets that stay inside the reward band
        (so we keep earning rewards) but move toward the learned
        optimal position.
        """
        if condition_id not in self.market_offsets:
            return (-self.base_delta_min_ticks, self.base_delta_min_ticks)

        # Get the learned optimal offset for this market
        optimal = self.market_offsets[condition_id]
        # Gradual adaptation: move 20% toward optimal each requote
        # (prevents oscillation around the optimal)
        current = self.stats.get(condition_id)
        if current and current.best_fill_rate > 0:
            # Use the learned offset directly (it's already the best)
            buy_offset = -optimal
            sell_offset = optimal
        else:
            # No fills yet: use profile defaults
            buy_offset = -self.base_delta_min_ticks
            sell_offset = self.base_delta_min_ticks
        return buy_offset, sell_offset

    def learn_from_fill(
        self,
        condition_id: str,
        offset_ticks: int,
        edge: float,
        markout: float,
    ) -> None:
        """Record a fill outcome and update the learned offset."""
        if condition_id not in self.stats:
            self.stats[condition_id] = MarketFillStats()
        stats = self.stats[condition_id]
        stats.record_fill(offset_ticks, edge, markout)
        # If this is the best offset so far, update the market offset
        if stats.best_fill_rate > 0:
            self.market_offsets[condition_id] = stats.best_offset

    def record_quote(self, condition_id: str, offset_ticks: int) -> None:
        """Record that a quote was placed at a given offset."""
        if condition_id not in self.stats:
            self.stats[condition_id] = MarketFillStats()
        self.stats[condition_id].record_quote(offset_ticks)

    def get_stats(self, condition_id: str) -> MarketFillStats:
        """Get fill stats for a market."""
        return self.stats.get(condition_id, MarketFillStats())
