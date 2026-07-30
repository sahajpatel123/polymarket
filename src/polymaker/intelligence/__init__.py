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
- SignalProcessor: Kalman filter, CUSUM change-point, HMM regime
- InformationProcessor: entropy, KL divergence, autocorrelation, TE
- PortfolioState: capital allocation, correlation, diversification
- RiskState: dynamic stop-loss, adaptive position limits
- SelfEvaluation: calibration, decay detection, PnL attribution
- SmartExecutor: timing, anti-gaming, iceberg sizing
- DeepSeekAgent: xAI Grok 4.5 reasoning client (tool-calling)
- AgentMemory: long-term SQLite memory
- OversightLoop: 30-min commentary + action queue
- MarketDiscovery: LLM-ranked Gamma market selection
- prompts: versioned LLM prompt templates

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
from polymaker.intelligence.agent import (
    DEFAULT_MODEL,
    AgentResponse,
    DeepSeekAgent,
    TokenUsage,
    ToolCall,
    function_tool,
)
from polymaker.intelligence.decision import (
    DecisionFramework,
    IntelligenceState,
    TradingDecision,
)
from polymaker.intelligence.deepseek_triggers import (
    DeepSeekTrigger,
    TriggerViolation,
    evaluate_triggers,
)
from polymaker.intelligence.discovery import (
    DiscoveryResult,
    MarketDiscovery,
    RankedMarket,
)
from polymaker.intelligence.execution import (
    AntiGamingDetector,
    IcebergSizer,
    OrderTimingOptimizer,
    SmartExecutor,
)
from polymaker.intelligence.governed_agent import GovernedDeepSeekAgent, GovernedResponse
from polymaker.intelligence.info_theory import (
    AutocorrelationTracker,
    EntropyTracker,
    InformationFeatures,
    InformationProcessor,
    KLDivergenceTracker,
    TransferEntropyTracker,
)
from polymaker.intelligence.llm_governance import (
    DEFAULT_DEAD_LLM_TIMEOUT_S,
    DEFAULT_LLM_DAILY_LOSS_PCT,
    DEFAULT_LLM_SIZE_MULT,
    DEFAULT_PAPER_SECONDS,
    FORBIDDEN_LLM_FIELDS,
    FORBIDDEN_LLM_PARAMS,
    SAFE_KNOB_RANGES,
    SAFE_KNOBS,
    GovernanceDecision,
    LLMDailyLoss,
    LLMGovernance,
    RewardEligibility,
)
from polymaker.intelligence.memory import AgentMemory
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
from polymaker.intelligence.orchestrator import (
    AllocationPlan,
    MarketAllocation,
    MarketCandidate,
    plan_allocations,
)
from polymaker.intelligence.oversight import (
    OversightAction,
    OversightLoop,
    OversightReport,
)
from polymaker.intelligence.policy import (
    ResolvedPolicy,
    RiskPolicy,
    RiskProfile,
    load_capital_usdc,
)
from polymaker.intelligence.portfolio import (
    MarketAllocationState,
    PortfolioState,
)
from polymaker.intelligence.profile_history import (
    ProfileChange,
    ProfileHistory,
)
from polymaker.intelligence.prompts import PROMPT_VERSION
from polymaker.intelligence.regime_detector import (
    FeatureExtractor,
    MarketFeatures,
    MarketRegime,
    RegimeDecision,
    detect_regime,
)
from polymaker.intelligence.resolution import (
    ALPHA_BIAS_THRESHOLD,
    ALPHA_DIRECTIONAL_THRESHOLD,
    ResolutionSignal,
    compute_alpha,
    estimate_resolution_probability,
)
from polymaker.intelligence.review import (
    DaySummary,
    LocalMemoryStore,
    ReviewResult,
    load_memory,
    render_markdown,
    run_daily_review,
    should_run_eod_review,
)
from polymaker.intelligence.risk import (
    AdaptivePositionLimit,
    DynamicStopLoss,
    RiskState,
)
from polymaker.intelligence.self_eval import (
    CalibrationTracker,
    PnLAttribution,
    SelfEvaluation,
    StrategyDecayDetector,
)
from polymaker.intelligence.self_improve import (
    FORBIDDEN_KEYS,
    SAFE_IMMEDIATE_KEYS,
    ImprovementSuggestion,
    ImproveResult,
    SelfImprover,
    apply_overrides,
    needs_improvement,
    parse_llm_json,
    strip_forbidden,
)
from polymaker.intelligence.signal_processing import (
    CUSUMDetector,
    KalmanMidPrice,
    SignalProcessor,
    VolatilityRegimeHMM,
    WaveletDenoiser,
)
from polymaker.intelligence.sizing import (
    SizingDecision,
    SizingParams,
    allocation_from_confidence,
    size_layers,
)

__all__ = [
    "ALPHA_BIAS_THRESHOLD",
    "ALPHA_DIRECTIONAL_THRESHOLD",
    "AdaptivePositionLimit",
    "AdaptiveSpreadParams",
    "AgentMemory",
    "AgentResponse",
    "AllocationPlan",
    "AntiGamingDetector",
    "AutocorrelationTracker",
    "CUSUMDetector",
    "CalibrationTracker",
    "DEFAULT_MODEL",
    "DaySummary",
    "DecisionFramework",
    "DiscoveryResult",
    "DynamicStopLoss",
    "EntropyTracker",
    "FeatureExtractor",
    "FORBIDDEN_KEYS",
    "FORBIDDEN_LLM_FIELDS",
    "FORBIDDEN_LLM_PARAMS",
    "DEFAULT_DEAD_LLM_TIMEOUT_S",
    "DEFAULT_LLM_DAILY_LOSS_PCT",
    "DEFAULT_LLM_SIZE_MULT",
    "DEFAULT_PAPER_SECONDS",
    "GovernanceDecision",
    "DeepSeekAgent",
    "GovernedDeepSeekAgent",
    "GovernedResponse",
    "DeepSeekTrigger",
    "TriggerViolation",
    "evaluate_triggers",
    "IcebergSizer",
    "ImprovementSuggestion",
    "ImproveResult",
    "InformationFeatures",
    "InformationProcessor",
    "IntelligenceState",
    "KLDivergenceTracker",
    "KalmanMidPrice",
    "LLMDailyLoss",
    "LLMGovernance",
    "LocalMemoryStore",
    "MarketAllocation",
    "MarketAllocationState",
    "MarketCandidate",
    "MarketDiscovery",
    "MarketFeatures",
    "MarketFillStats",
    "MarketRegime",
    "MicrostructureFeatures",
    "MicrostructureTracker",
    "OrderTimingOptimizer",
    "OversightAction",
    "OversightLoop",
    "OversightReport",
    "PROMPT_VERSION",
    "PnLAttribution",
    "PortfolioState",
    "ProfileChange",
    "ProfileHistory",
    "PROMPT_VERSION",
    "RankedMarket",
    "RegimeDecision",
    "ResolvedPolicy",
    "ResolutionSignal",
    "RewardEligibility",
    "ReviewResult",
    "RiskPolicy",
    "RiskProfile",
    "RiskState",
    "SAFE_IMMEDIATE_KEYS",
    "SAFE_KNOB_RANGES",
    "SAFE_KNOBS",
    "SelfEvaluation",
    "SelfImprover",
    "SignalProcessor",
    "SizingDecision",
    "SizingParams",
    "SmartExecutor",
    "StrategyDecayDetector",
    "TokenUsage",
    "ToolCall",
    "TradingDecision",
    "TransferEntropyTracker",
    "VolatilityRegimeHMM",
    "WaveletDenoiser",
    "allocation_from_confidence",
    "apply_overrides",
    "compute_alpha",
    "compute_adverse_selection_risk",
    "compute_depth_imbalance",
    "compute_fill_probability",
    "compute_flow_imbalance",
    "compute_microprice",
    "compute_opportunity_score",
    "compute_realized_vol",
    "detect_regime",
    "estimate_resolution_probability",
    "function_tool",
    "load_capital_usdc",
    "load_memory",
    "needs_improvement",
    "parse_llm_json",
    "plan_allocations",
    "render_markdown",
    "run_daily_review",
    "should_run_eod_review",
    "size_layers",
    "strip_forbidden",
]
