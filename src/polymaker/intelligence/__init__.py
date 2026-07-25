"""Intelligence layer: adaptive parameters, regime detection, decision framework.

This module adds a "brain" to the project — a set of components that
learn from observed outcomes and reason about trading decisions,
rather than relying purely on fixed rules.

Components:
- AdaptiveSpreadParams: learns optimal band position from fill outcomes
- MarketFeatures + detect_regime: classifies current market conditions
- FeatureExtractor: extracts microstructure features from raw data
- MicrostructureFeatures: microprice, flow imbalance, queue position
- IntelligenceState: per-market state combining all components
- DecisionFramework: top-level decision engine
- TradingDecision: the output of the decision framework

All components are pure functions / state machines — no I/O. The
engine feeds them raw data and queries decisions.

Usage in the engine:
    from polymaker.intelligence import DecisionFramework

    framework = DecisionFramework(max_active_markets=5)
    # On each book update:
    framework.update_features(cid, features)
    framework.update_microstructure(cid, best_bid, best_ask, depth, depth, ts)
    # Before each requote:
    decision = framework.decide(cid)
    if decision.should_quote:
        place_orders(decision.buy_offset_ticks, decision.sell_offset_ticks, ...)
    # After each fill:
    framework.record_fill(cid, offset_ticks, edge, markout)
"""

from polymaker.intelligence.adaptive_spread import (
    AdaptiveSpreadParams,
    MarketFillStats,
)
from polymaker.intelligence.decision import (
    DecisionFramework,
    IntelligenceState,
    TradingDecision,
)
from polymaker.intelligence.microstructure import (
    MicrostructureFeatures,
    MicrostructureTracker,
    compute_adverse_selection_risk,
    compute_depth_imbalance,
    compute_fill_probability,
    compute_flow_imbalance,
    compute_microprice,
    compute_opportunity_score,
    compute_realized_vol,
)
from polymaker.intelligence.regime_detector import (
    FeatureExtractor,
    MarketFeatures,
    MarketRegime,
    RegimeDecision,
    detect_regime,
)

__all__ = [
    "AdaptiveSpreadParams",
    "DecisionFramework",
    "FeatureExtractor",
    "IntelligenceState",
    "MarketFeatures",
    "MarketFillStats",
    "MarketRegime",
    "MicrostructureFeatures",
    "MicrostructureTracker",
    "RegimeDecision",
    "TradingDecision",
    "compute_adverse_selection_risk",
    "compute_depth_imbalance",
    "compute_fill_probability",
    "compute_flow_imbalance",
    "compute_microprice",
    "compute_opportunity_score",
    "compute_realized_vol",
    "detect_regime",
]
