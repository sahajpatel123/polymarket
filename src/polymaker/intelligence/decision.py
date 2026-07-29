"""Decision framework: the brain that reasons about trading decisions.

This is the top-level intelligence layer that combines:
- Adaptive spread parameters (learned from fills)
- Market regime detection (quiet/trending/volatile/toxic)
- Microstructure features (microprice, flow, depth)
- Market opportunity scoring (which market deserves capital)

The decision framework answers: "Should I place orders on this market
right now, and at what price?"

Decision flow:
  1. Regime check: is the market dead, stale, toxic, volatile?
     → If dead/stale: skip
     → If toxic: widen spread 2x, half size
     → If volatile: widen spread 1.5x, half size
  2. Adaptive spread: where have past fills occurred on this market?
     → Use the learned optimal offset (gradual adaptation)
  3. Microstructure check: is the current book favorable?
     → Depth imbalance in our direction: increase fill probability
     → Adverse selection risk high: widen spread
  4. Opportunity ranking: if capital is limited, which market gets it?
     → Rank by opportunity_score (fill_prob * edge * (1 - as_risk))
  5. Final decision: should_quote, spread, size, band position

Pure functions only — no I/O. The engine calls decide() on each
requote with the latest intelligence state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polymaker.intelligence.adaptive_spread import (
    AdaptiveSpreadParams,
)
from polymaker.intelligence.microstructure import (
    MicrostructureTracker,
)
from polymaker.intelligence.regime_detector import (
    MarketFeatures,
    MarketRegime,
    detect_regime,
)


@dataclass
class TradingDecision:
    """Final decision for a market: should we quote, and how.

    This is what the engine reads to set its quote parameters.
    """

    market_id: str
    should_quote: bool
    regime: MarketRegime
    # Spread parameters
    spread_multiplier: float = 1.0
    # Offset from FV in ticks (BUY negative = below FV; SELL positive = above)
    buy_offset_ticks: int = 0
    sell_offset_ticks: int = 0
    # Size parameters
    size_multiplier: float = 1.0
    # Where in the reward band to rest BUY: 0.0 = band floor (passive),
    # 1.0 = near FV − min_edge (aggressive, more fills). Learned + regime.
    buy_band_frac: float = 0.5  # mid-band, where trades actually occur
    # Intelligence diagnostics
    opportunity_score: float = 0.0
    expected_edge: float = 0.0
    fill_probability: float = 0.0
    adverse_selection_risk: float = 0.0
    # Reason
    reason: str = ""


@dataclass
class IntelligenceState:
    """All intelligence state for a single market.

    Combines:
    - Regime detection state (features + decision)
    - Adaptive spread parameters (learned from fills)
    - Microstructure tracker (order book + trade history)
    """

    condition_id: str
    regime_features: MarketFeatures = field(default_factory=MarketFeatures)
    adaptive: AdaptiveSpreadParams = field(default_factory=AdaptiveSpreadParams)
    microstructure: MicrostructureTracker = field(
        default_factory=MicrostructureTracker
    )
    # Latest decision (cached)
    last_decision: TradingDecision | None = None
    # Decision counter (for logging)
    n_decisions: int = 0


@dataclass
class DecisionFramework:
    """Top-level decision framework for all markets.

    Maintains IntelligenceState per market and provides decide() that
    combines regime detection, adaptive spread, and microstructure.
    """

    states: dict[str, IntelligenceState] = field(default_factory=dict)
    # Capital limit: if set, rank markets and only quote top N
    max_active_markets: int = 0  # 0 = unlimited

    def get_state(self, condition_id: str) -> IntelligenceState:
        """Get or create intelligence state for a market."""
        if condition_id not in self.states:
            self.states[condition_id] = IntelligenceState(
                condition_id=condition_id
            )
        return self.states[condition_id]

    def update_features(
        self,
        condition_id: str,
        features: MarketFeatures,
    ) -> None:
        """Update regime features for a market."""
        state = self.get_state(condition_id)
        state.regime_features = features

    def record_fill(
        self,
        condition_id: str,
        offset_ticks: int,
        edge: float,
        markout: float,
    ) -> None:
        """Record a fill outcome and update adaptive spread."""
        state = self.get_state(condition_id)
        state.adaptive.learn_from_fill(
            condition_id, offset_ticks, edge, markout
        )

    def record_quote(
        self,
        condition_id: str,
        offset_ticks: int,
    ) -> None:
        """Record that a quote was placed at a given offset."""
        state = self.get_state(condition_id)
        state.adaptive.record_quote(condition_id, offset_ticks)

    def update_microstructure(
        self,
        condition_id: str,
        best_bid: float, best_ask: float,
        bid_depth: float, ask_depth: float,
        ts: float,
    ) -> None:
        """Update microstructure tracker for a market."""
        state = self.get_state(condition_id)
        state.microstructure.update_book(
            best_bid, best_ask, bid_depth, ask_depth, ts
        )

    def update_trade(
        self,
        condition_id: str,
        side: str, price: float, size: float, ts: float,
    ) -> None:
        """Record a trade for microstructure analysis."""
        state = self.get_state(condition_id)
        state.microstructure.update_trade(side, price, size, ts)

    def decide(self, condition_id: str) -> TradingDecision:
        """Make a trading decision for a market.

        Combines:
        1. Regime detection (should we quote at all?)
        2. Adaptive spread (where should we place orders?)
        3. Microstructure (is the current book favorable?)
        4. Opportunity ranking (which market gets capital?)
        """
        state = self.get_state(condition_id)
        state.n_decisions += 1

        # 1. Regime detection
        regime_decision = detect_regime(state.regime_features)

        if not regime_decision.should_quote:
            decision = TradingDecision(
                market_id=condition_id,
                should_quote=False,
                regime=regime_decision.regime,
                reason=regime_decision.skip_reason,
            )
            state.last_decision = decision
            return decision

        # 2. Adaptive spread: learned optimal offset for this market
        buy_offset, sell_offset = state.adaptive.get_band_position(
            condition_id
        )

        # 3. Microstructure check
        micro_features = state.microstructure.extract()

        # Adjust offsets based on microstructure
        # If depth imbalance is in our direction: move closer to mid
        # If adverse selection risk is high: move away from mid
        flow = micro_features.flow_imbalance
        as_risk = micro_features.adverse_selection_risk

        # If BUY: flow against us (negative) = good (price likely to drop)
        # If SELL: flow with us (positive) = good (price likely to rise)
        buy_flow_signal = -flow  # positive if flow is bearish
        sell_flow_signal = flow  # positive if flow is bullish

        # Adjust offsets: positive signal = move closer to mid (1 tick)
        if buy_flow_signal > 0.1:
            buy_offset = min(buy_offset + 1, 0)  # move toward 0 (closer to mid)
        if sell_flow_signal > 0.1:
            sell_offset = max(sell_offset - 1, 0)

        # Adverse selection risk: move away from mid
        if as_risk > 0.5:
            # Make BUY lower and SELL higher (more cautious)
            buy_offset = min(buy_offset, -1)
            sell_offset = max(sell_offset, 1)

        # 4. Opportunity score
        fill_prob = (micro_features.fill_probability_buy
                     + micro_features.fill_probability_sell) / 2
        expected_edge = 0.005  # 0.5¢
        opportunity = micro_features.opportunity_score

        # 5. Apply regime multipliers
        spread_mult = regime_decision.spread_multiplier
        size_mult = regime_decision.size_multiplier

        # 6. Band position: how aggressive inside the reward band
        # Start at mid-band (0.5) so fills actually happen — trading occurs
        # in the upper half. Toxic/AS pulls us down; good edge pushes up.
        buy_band_frac = 0.5
        stats = state.adaptive.get_stats(condition_id)
        if stats.n_fills > 0:
            avg_m = stats.get_avg_markout()
            if avg_m < -0.005:
                buy_band_frac = 0.0
            elif avg_m > 0.002:
                buy_band_frac = min(0.8, 0.5 + 0.1 * stats.n_fills)
        # Only push to floor when AS/toxicity is genuinely high
        if as_risk > 0.5 or state.regime_features.toxicity > 0.05:
            buy_band_frac = min(buy_band_frac, 0.05)
        if regime_decision.regime in (MarketRegime.TOXIC, MarketRegime.VOLATILE):
            buy_band_frac = min(buy_band_frac, 0.1)
            size_mult = min(size_mult, 0.5)
        if buy_flow_signal > 0.2 and as_risk < 0.3:
            buy_band_frac = min(0.8, buy_band_frac + 0.2)

        decision = TradingDecision(
            market_id=condition_id,
            should_quote=True,
            regime=regime_decision.regime,
            spread_multiplier=spread_mult,
            buy_offset_ticks=buy_offset,
            sell_offset_ticks=sell_offset,
            size_multiplier=size_mult,
            buy_band_frac=buy_band_frac,
            opportunity_score=opportunity,
            expected_edge=expected_edge,
            fill_probability=fill_prob,
            adverse_selection_risk=as_risk,
            reason=(
                f"regime={regime_decision.regime.value} "
                f"buy_off={buy_offset} sell_off={sell_offset} "
                f"band_frac={buy_band_frac:.2f} as_risk={as_risk:.2f}"
            ),
        )
        state.last_decision = decision
        return decision

    def rank_markets(
        self, condition_ids: list[str]
    ) -> list[str]:
        """Rank markets by opportunity score, best first.

        Used when capital is limited and we need to pick the best
        markets to quote on.
        """
        scores = []
        for cid in condition_ids:
            state = self.get_state(cid)
            if state.last_decision:
                scores.append((cid, state.last_decision.opportunity_score))
            else:
                scores.append((cid, 0.0))
        scores.sort(key=lambda x: -x[1])
        return [cid for cid, _ in scores]

    def get_active_markets(self) -> list[str]:
        """Get the set of markets we should be quoting on.

        If max_active_markets is set, returns the top N by opportunity score.
        Otherwise returns all markets that should_quote.
        """
        should_quote = []
        for cid, state in self.states.items():
            if state.last_decision and state.last_decision.should_quote:
                should_quote.append((cid, state.last_decision.opportunity_score))
        should_quote.sort(key=lambda x: -x[1])
        if self.max_active_markets > 0:
            return [cid for cid, _ in should_quote[:self.max_active_markets]]
        return [cid for cid, _ in should_quote]
