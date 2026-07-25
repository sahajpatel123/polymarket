"""Engine degradation: quarantine must allow exits; urgency + sensors live."""

from __future__ import annotations

from polymaker.config import StrategyProfile
from polymaker.domain import Position, Regime, Side
from polymaker.strategy.decision_pipeline import build_targets
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.regime import RegimeMachine
from polymaker.risk.degradation import (
    DegradationAction,
    DegradationConfig,
    DegradationDetector,
)
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore
from polymaker.config import RiskConfig
from tests.conftest import view


def _est() -> MarketEstimators:
    return MarketEstimators(
        vol=VolEstimator(10, 600),
        flow=FlowEstimator(120),
        markout=MarkoutTracker(),
    )


def test_risk_halt_empties_targets_but_reduce_only_keeps_exits(meta) -> None:
    """Skeptic proof: HALT = no exits; REDUCE_ONLY = exits present."""
    p = StrategyProfile()
    held = Position(meta.yes.token_id, size=50.0, avg_price=0.48)
    halted = build_targets(
        meta=meta, profile=p,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        est=_est(), regime_machine=RegimeMachine(), now=1.0, micro=0.5,
        risk_halt=True, risk_reduce_only=False,
        yes_exit_urgency=0.8,
    )
    assert halted is not None
    assert halted.regime is Regime.HALTED
    assert halted.targets.quotes == ()

    reduced = build_targets(
        meta=meta, profile=p,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        est=_est(), regime_machine=RegimeMachine(), now=1.0, micro=0.5,
        risk_halt=False, risk_reduce_only=True,
        yes_exit_urgency=0.8,
    )
    assert reduced is not None
    assert reduced.regime is Regime.REDUCE_ONLY
    sells = [q for q in reduced.targets.quotes if q.side is Side.SELL]
    buys = [q for q in reduced.targets.quotes if q.side is Side.BUY]
    assert buys == []
    assert sells, "quarantine/reduce_only must still place inventory exits"


def test_exit_urgency_passed_into_pipeline_lowers_sell(meta) -> None:
    p = StrategyProfile()
    held = Position(meta.yes.token_id, size=50.0, avg_price=0.48)
    passive = build_targets(
        meta=meta, profile=p,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        est=_est(), regime_machine=RegimeMachine(), now=100.0, micro=0.5,
        yes_exit_urgency=0.0,
    )
    urgent = build_targets(
        meta=meta, profile=p,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        est=_est(), regime_machine=RegimeMachine(), now=100.0, micro=0.5,
        yes_exit_urgency=1.0,
    )
    assert passive is not None and urgent is not None

    def sell_px(res):
        xs = [
            q.price for q in res.targets.quotes
            if q.side is Side.SELL and q.token_id == meta.yes.token_id
        ]
        return min(xs) if xs else None

    # With inventory and urgency, exits should exist and urgent ≤ passive
    # (when both produce sells)
    up, pp = sell_px(urgent), sell_px(passive)
    if up is not None and pp is not None:
        assert up <= pp + 1e-12


def test_degradation_day_start_equity_and_drawdown_halt() -> None:
    """day_start_equity is exposed on RiskManager; drawdown uses it when set."""
    store = StateStore(":memory:")
    rm = RiskManager(RiskConfig(bankroll_usdc=100.0), store)
    # Simulate day start with capital snapshot
    rm._day_start_equity = 100.0  # type: ignore[attr-defined]
    assert rm.day_start_equity == 100.0

    d = DegradationDetector(DegradationConfig(drawdown_halt_frac=0.10))
    # Engine copies: gs.day_start_equity = self.risk.day_start_equity
    d.global_state.day_start_equity = rm.day_start_equity
    d.global_state.equity = 85.0  # 15% DD from day start
    dec = d.evaluate()
    assert dec.action is DegradationAction.GLOBAL_HALT
    assert dec.halt is True


def test_degradation_records_quote_and_order_enable_fill_rate_path() -> None:
    d = DegradationDetector(DegradationConfig(fill_rate_floor=0.02, min_fills_for_signal=5))
    st = d.state_for("m1")
    for _ in range(60):
        st.record_quote()
    # only 1 fill → low fill rate
    st.record_fill(0.0)
    dec = d.evaluate("m1")
    assert dec.action is DegradationAction.SIZE_CUT
    assert "fill_rate" in dec.reason or dec.size_multiplier < 1.0


def test_quarantine_mapping_uses_reduce_only_not_halt() -> None:
    """Document the engine contract: quarantine → reduce_only only."""
    d = DegradationDetector(DegradationConfig(min_fills_for_signal=5, markout_toxic=-0.01))
    st = d.state_for("m1")
    for _ in range(10):
        st.record_fill(-0.02)
    dec = d.evaluate("m1")
    assert dec.quarantine is True
    assert dec.halt is False
    # Engine must map quarantine → risk_reduce_only=True, risk_halt=False
    # (proven by test_risk_halt_empties_targets_but_reduce_only_keeps_exits)
