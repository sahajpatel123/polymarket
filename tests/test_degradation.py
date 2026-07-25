"""Degradation detector: cut size / baseline / quarantine / halt."""

from __future__ import annotations

from polymaker.risk.degradation import (
    DegradationAction,
    DegradationConfig,
    DegradationDetector,
)


def test_healthy_default() -> None:
    d = DegradationDetector()
    dec = d.evaluate("m1")
    assert dec.action is DegradationAction.NONE
    assert dec.size_multiplier == 1.0


def test_toxic_markout_quarantines() -> None:
    d = DegradationDetector(DegradationConfig(min_fills_for_signal=5, markout_toxic=-0.01))
    st = d.state_for("m1")
    for _ in range(10):
        st.record_fill(-0.02)
    dec = d.evaluate("m1")
    assert dec.action is DegradationAction.MARKET_QUARANTINE
    assert dec.quarantine is True
    assert dec.size_multiplier == 0.0


def test_drawdown_halts() -> None:
    d = DegradationDetector(DegradationConfig(drawdown_halt_frac=0.10))
    d.global_state.day_start_equity = 100.0
    d.global_state.equity = 85.0  # 15% DD
    dec = d.evaluate()
    assert dec.action is DegradationAction.GLOBAL_HALT
    assert dec.halt is True


def test_low_intel_confidence_baseline_fallback() -> None:
    d = DegradationDetector(DegradationConfig(min_fills_for_signal=5, markout_warn=-0.003))
    st = d.state_for("m1")
    for _ in range(6):
        st.record_fill(-0.005)
    dec = d.evaluate("m1", intelligence_confidence=0.1)
    assert dec.action is DegradationAction.BASELINE_FALLBACK
    assert dec.use_baseline_profile is True
    assert dec.size_multiplier < 1.0


def test_consecutive_toxic_quarantine() -> None:
    d = DegradationDetector()
    st = d.state_for("m1")
    for _ in range(8):
        st.record_fill(-0.01)
    dec = d.evaluate("m1")
    assert dec.action is DegradationAction.MARKET_QUARANTINE
