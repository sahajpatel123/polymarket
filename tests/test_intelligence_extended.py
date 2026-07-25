"""Tests for the new intelligence components: signal processing,
information theory, portfolio, risk, self-evaluation, execution.
"""

from __future__ import annotations

import math

from polymaker.intelligence.execution import (
    AntiGamingDetector,
    IcebergSizer,
    OrderTimingOptimizer,
    SmartExecutor,
)
from polymaker.intelligence.info_theory import (
    AutocorrelationTracker,
    EntropyTracker,
    InformationFeatures,
    InformationProcessor,
    KLDivergenceTracker,
    TransferEntropyTracker,
)
from polymaker.intelligence.portfolio import (
    MarketAllocationState,
    PortfolioState,
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
from polymaker.intelligence.signal_processing import (
    CUSUMDetector,
    KalmanMidPrice,
    SignalProcessor,
    VolatilityRegimeHMM,
    WaveletDenoiser,
)


# ── Signal Processing tests ───────────────────────────────────────────


def test_kalman_basic():
    """Kalman filter should track noisy mid-price with smoothing."""
    k = KalmanMidPrice(Q=0.0001, R=0.0001)
    # Feed 10 observations around 0.50
    for i in range(10):
        x_hat, P = k.update(0.50 + (i * 0.001))
    # x_hat should be near 0.51
    assert 0.50 < k.x_hat < 0.52
    # P should be smaller than initial
    assert k.P < 1.0
    # Uncertainty should be smaller
    assert k.uncertainty() < 1.0


def test_kalman_uncertainty_decreases():
    """More observations should reduce uncertainty."""
    k1 = KalmanMidPrice()
    k2 = KalmanMidPrice()
    for _ in range(10):
        k1.update(0.50)
    for _ in range(100):
        k2.update(0.50)
    assert k1.uncertainty() > k2.uncertainty()


def test_cusum_detects_change():
    """CUSUM should detect a sudden shift in the signal."""
    c = CUSUMDetector(h=0.01, k=0.001)
    # Feed 100 values at 0 (no change)
    for _ in range(100):
        drift, detected = c.update(0.0)
        assert not detected
    # Now feed 50 values at 0.02 (shift)
    for _ in range(50):
        drift, detected = c.update(0.02)
    assert detected


def test_cusum_reset():
    """CUSUM should reset after detection."""
    c = CUSUMDetector(h=0.01, k=0.001)
    # Feed 100 values at 0 (no change)
    for _ in range(100):
        c.update(0.0)
    # Now feed 50 values at 0.02 (shift)
    detected = False
    for _ in range(50):
        drift, d = c.update(0.02)
        if d:
            detected = True
            break
    assert detected
    # Reset should clear accumulators
    c.reset()
    assert c.S_pos == 0.0
    assert c.S_neg == 0.0


def test_hmm_basic():
    """HMM should classify high-vol state correctly."""
    hmm = VolatilityRegimeHMM(sigma_low=0.001, sigma_high=0.01)
    # Feed calm observations
    for _ in range(20):
        hmm.update(0.50)
    # After calm, P(low-vol) should be high
    assert hmm.alpha[0] > 0.5


def test_signal_processor_combines():
    """SignalProcessor combines all sub-components."""
    sp = SignalProcessor()
    for i in range(30):
        sp.update_mid(0.50 + (i * 0.001))
    features = sp.extract()
    assert "kalman_mid" in features
    assert "kalman_uncertainty" in features
    assert "hmm_p_low_vol" in features
    assert features["n_updates"] == 30


# ── Information Theory tests ────────────────────────────────────────


def test_entropy_tracker_basic():
    """Entropy should be 0 for deterministic, high for random."""
    e = EntropyTracker()
    # All same value = 0 entropy
    for _ in range(10):
        e.update(0.50)
    assert e.entropy() == 0.0
    # Reset and feed random values
    e2 = EntropyTracker()
    for i in range(100):
        e2.update(0.50 + (i % 5) * 0.001)  # 5 distinct values
    assert e2.entropy() > 0.0
    assert e2.normalized_entropy() > 0.0


def test_kl_divergence():
    """KL divergence should be 0 for identical distributions."""
    k = KLDivergenceTracker()
    for _ in range(100):
        k.update(0.50, is_reference=True)
        k.update(0.50, is_reference=False)
    assert k.divergence() < 0.1


def test_autocorrelation_momentum():
    """Positive autocorrelation for momentum."""
    a = AutocorrelationTracker(n_lags=3)
    # Feed 100 values with strong positive trend
    for i in range(100):
        a.update(0.50 + i * 0.01)
    acs = a.autocorrelations()
    # Lag-1 autocorrelation should be positive (momentum)
    assert acs[0] > 0


def test_transfer_entropy():
    """TE should be positive when flow predicts price."""
    t = TransferEntropyTracker()
    # Feed 50 values with strong flow -> price correlation
    for i in range(50):
        flow = 0.01 if i % 2 == 0 else -0.01
        mid = 0.50 + (i * 0.001) * (1 if flow > 0 else -1)
        t.update(mid, flow)
    te = t.transfer_entropy()
    assert te != 0.0


def test_information_processor_combines():
    """InformationProcessor combines all sub-components."""
    ip = InformationProcessor()
    for i in range(50):
        ip.update(0.50 + (i * 0.001), flow=0.01)
    features = ip.extract()
    assert features.n_observations == 50
    assert features.entropy_nats >= 0


# ── Portfolio tests ─────────────────────────────────────────────────


def test_portfolio_rebalance_basic():
    """PortfolioState should compute target allocations."""
    p = PortfolioState(total_capital=100.0, max_concentration=1.0)
    p.update_market("A", expected_return=20.0, risk=0.01)
    p.update_market("B", expected_return=10.0, risk=0.01)
    targets = p.rebalance(0.0)
    total = sum(targets.values())
    assert abs(total - 100.0) < 1.0
    # Higher expected return = more allocation
    assert targets.get("A", 0) > targets.get("B", 0)


def test_portfolio_rebalance_empty():
    """Empty portfolio should return empty dict."""
    p = PortfolioState()
    targets = p.rebalance(0.0)
    assert targets == {}


def test_portfolio_max_concentration():
    """No single market should exceed max_concentration of total capital."""
    p = PortfolioState(total_capital=100.0, max_concentration=0.3)
    p.update_market("A", expected_return=100.0, risk=0.01)
    p.update_market("B", expected_return=1.0, risk=0.01)
    targets = p.rebalance(0.0)
    # No market should exceed 30% of total capital
    for v in targets.values():
        assert v <= 30.0 + 1.0  # tolerance


def test_portfolio_correlation_tracking():
    """PortfolioState should track correlations between markets."""
    p = PortfolioState()
    p.update_correlation("A", "B", 0.7)
    assert p.markets["A"].correlation["B"] == 0.7
    assert p.markets["B"].correlation["A"] == 0.7


def test_portfolio_sharpe_ratio():
    """Sharpe ratio should be computed correctly."""
    p = PortfolioState(total_capital=100.0)
    p.update_market("A", expected_return=0.5, risk=0.1)
    p.update_market("B", expected_return=0.3, risk=0.1)
    p.markets["A"].current_allocation = 50.0
    p.markets["B"].current_allocation = 50.0
    sharpe = p.sharpe_ratio(n_days=1.0)
    assert sharpe > 0


# ── Risk tests ──────────────────────────────────────────────────────


def test_dynamic_stop_loss_basic():
    """Stop-loss should trigger when PnL drops below threshold."""
    s = DynamicStopLoss(min_threshold=10.0, k=3.0, time_window_s=1.0)
    # Big loss vs small threshold = stop (10.0 vs 3 * 0.01 * 1 * 100 = 3.0)
    assert s.should_stop(pnl=-20.0, vol_short=0.01)
    # Small loss = no stop
    assert not s.should_stop(pnl=-1.0, vol_short=0.0001)


def test_adaptive_position_limit_scaling():
    """Position limit should scale with PnL and volatility."""
    p = AdaptivePositionLimit(base_limit=100.0)
    # High PnL = higher limit
    limit_high_pnl = p.compute_limit(current_pnl=10.0, vol_short=0.001, current_exposure=10.0)
    # Low PnL = lower limit
    limit_low_pnl = p.compute_limit(current_pnl=-5.0, vol_short=0.001, current_exposure=10.0)
    assert limit_high_pnl > limit_low_pnl


def test_adaptive_position_limit_min():
    """Position limit should not go below min_limit."""
    p = AdaptivePositionLimit(base_limit=100.0, min_limit=10.0)
    limit = p.compute_limit(current_pnl=-100.0, vol_short=1.0, current_exposure=1000.0)
    assert limit >= p.min_limit


def test_risk_state_combines():
    """RiskState should combine stop-loss and position limit."""
    r = RiskState()
    r.update(pnl=-5.0, exposure=20.0, vol_short=0.0001)
    # Large enough loss to trigger the min_threshold stop
    r.stop_loss = DynamicStopLoss(min_threshold=1.0, k=1.0, time_window_s=1.0)
    assert r.should_stop_loss()  # Big loss
    limit = r.compute_position_limit()
    assert limit > 0


# ── Self-Evaluation tests ─────────────────────────────────────────


def test_calibration_tracker_basic():
    """CalibrationTracker should track predicted vs actual."""
    c = CalibrationTracker()
    c.update_prediction(fill_rate=0.5, edge=0.01, as_rate=0.0)
    c.record_quote()
    c.record_fill(edge=0.005, as_observed=-0.002)
    assert c.fill_rate() == 1.0  # 1 fill in 1 quote
    assert c.fill_rate_calibration() == 0.5  # predicted 0.5, actual 1.0


def test_strategy_decay_detector():
    """Decay detector should flag when rolling Sharpe < threshold."""
    s = StrategyDecayDetector(decay_threshold=0.0, max_consecutive=2, rolling_window=10)
    # Need a full rolling window, then enough further updates for max_consecutive
    for i in range(12):
        s.update(-1.0 + (i * 0.01))  # slight variance, overall negative
    assert s.consecutive_periods >= 2
    assert s.is_decaying()


def test_strategy_decay_recovery():
    """Decay detector should reset on good trade."""
    s = StrategyDecayDetector(decay_threshold=0.0, max_consecutive=2)
    s.update(-1.0)
    s.update(-1.0)
    s.update(1.0)  # Good trade resets
    assert s.consecutive_periods == 0


def test_pnl_attribution():
    """PnLAttribution should track decisions by regime/offset."""
    a = PnLAttribution()
    a.record_decision("QUIET", "BUY_2", 0.01)
    a.record_decision("QUIET", "BUY_2", -0.005)
    a.record_decision("TRENDING", "SELL_3", 0.02)
    assert a.n_decisions == 3
    assert a.n_profitable == 2
    assert a.regime_pnl()["QUIET"] == 0.005
    assert a.regime_pnl()["TRENDING"] == 0.02


def test_pnl_attribution_profit_factor():
    """Profit factor should be ratio of winning to losing PnL."""
    a = PnLAttribution()
    a.record_decision("QUIET", "BUY_2", 1.0)  # win
    a.record_decision("QUIET", "BUY_2", -0.5)  # loss
    assert abs(a.profit_factor() - 2.0) < 0.01


def test_self_evaluation_combines():
    """SelfEvaluation should combine all sub-components."""
    s = SelfEvaluation()
    s.update(pnl=0.01, regime="QUIET", offset="BUY_2")
    s.update(pnl=-0.005, regime="QUIET", offset="BUY_2")
    summary = s.summary()
    assert "hit_rate" in summary
    assert "avg_pnl" in summary
    assert "current_sharpe" in summary


# ── Execution tests ────────────────────────────────────────────────


def test_order_timing_optimizer():
    """OrderTimingOptimizer should defer quotes within min_jitter."""
    o = OrderTimingOptimizer(min_jitter_s=1.0)
    # First quote is always OK
    assert o.should_quote_now(100.0)
    o.record_quote(100.0)
    # Immediate second quote should be deferred
    assert not o.should_quote_now(100.5)
    # After jitter window, OK again
    assert o.should_quote_now(102.0)


def test_anti_gaming_detector():
    """AntiGamingDetector should flag high adverse selection rate."""
    a = AntiGamingDetector(threshold=0.7)
    # Feed 10 losing fills to push gaming score above threshold
    for _ in range(10):
        a.record_fill(markout=-0.01)
    assert a.is_gaming()
    assert a.recommended_spread_mult() > 1.0


def test_anti_gaming_no_gaming():
    """AntiGamingDetector should not flag good fills."""
    a = AntiGamingDetector(threshold=0.7)
    for _ in range(10):
        a.record_fill(markout=0.01)  # All winning
    assert not a.is_gaming()


def test_iceberg_sizer():
    """IcebergSizer should plan chunks and track progress."""
    i = IcebergSizer()
    n_chunks = i.plan(total=100.0, displayed=10.0)
    assert n_chunks == 10
    # Get first chunk
    assert i.next_chunk() == 10.0
    assert abs(i.progress() - 0.1) < 0.01
    # Get last chunk
    for _ in range(9):
        i.next_chunk()
    assert i.next_chunk() == 0.0
    assert i.progress() == 1.0


def test_smart_executor_combines():
    """SmartExecutor should combine timing, anti-gaming, iceberg."""
    s = SmartExecutor()
    s.timing.last_quote_ts = 0.0
    # No anti-gaming, timing OK → should quote
    should, reason = s.should_quote(10.0)
    assert should
    # Now trigger anti-gaming
    for _ in range(10):
        s.anti_gaming.record_fill(markout=-0.01)
    s.timing.last_quote_ts = 0.0
    should, reason = s.should_quote(10.0)
    assert not should
    assert reason == "anti_gaming"


def test_smart_executor_spread_multiplier():
    """SmartExecutor should widen spread when gaming detected."""
    s = SmartExecutor()
    for _ in range(10):
        s.anti_gaming.record_fill(markout=-0.01)
    mult = s.get_spread_multiplier()
    assert mult > 1.0
