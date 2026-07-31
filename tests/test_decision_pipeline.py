"""Shared decision pipeline used by live and replay."""

from __future__ import annotations

from polymaker.config import StrategyProfile
from polymaker.domain import Position, Regime
from polymaker.intelligence import DecisionFramework
from polymaker.strategy.decision_pipeline import build_targets
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.regime import RegimeMachine
from tests.conftest import view


def _est() -> MarketEstimators:
    return MarketEstimators(
        vol=VolEstimator(10, 600),
        flow=FlowEstimator(120),
        markout=MarkoutTracker(),
    )


def test_pipeline_produces_quotes_without_intel(meta, profile) -> None:
    yv = view(0.48, 0.52)
    res = build_targets(
        meta=meta,
        profile=profile,
        yes_view=yv,
        no_view=view(0.48, 0.52),
        pos_yes=Position(meta.yes.token_id),
        pos_no=Position(meta.no.token_id),
        est=_est(),
        regime_machine=RegimeMachine(),
        now=1.0,
        micro=0.50,
        intel=None,
    )
    assert res is not None
    assert res.attribution.intelligence_decision == "OFF"
    assert res.regime is not Regime.HALTED
    assert res.fv > 0


def test_pipeline_band_position_learns_from_regime(meta) -> None:
    """Unified regime machine sets band position from regime + toxicity."""
    p = StrategyProfile(use_intelligence=True)
    from dataclasses import replace

    m = replace(meta, rewards_daily_rate=0.0)
    rm = RegimeMachine()
    # Inject a toxic fill: toxicity=0.3 with fill history
    rm.record_fill(-3, 0.005, 0.005)  # fill at -3 ticks with positive edge
    # Create a mock estimator with high toxicity
    from polymaker.strategy.estimators import MarketEstimators, VolEstimator, FlowEstimator
    from polymaker.strategy.estimators import MultiHorizonMarkout
    toxic_markout = MultiHorizonMarkout()
    toxic_est = MarketEstimators(
        vol=VolEstimator(10, 600),
        flow=FlowEstimator(120),
        markout=toxic_markout,
    )
    res = build_targets(
        meta=m,
        profile=p,
        yes_view=view(0.48, 0.52),
        no_view=view(0.48, 0.52),
        pos_yes=Position(m.yes.token_id),
        pos_no=Position(m.no.token_id),
        est=toxic_est,
        regime_machine=rm,
        now=1.0,
        micro=0.50,
        n_trades_last_hour=0,
        seconds_since_last_trade=3600,
    )
    assert res is not None
    assert res.attribution.buy_band_frac is not None
    assert 0.0 <= res.attribution.buy_band_frac <= 1.0
    assert res.attribution.intelligence_decision in ("QUOTE", "SKIP")


def test_pipeline_attribution_fields(meta, profile) -> None:
    p = StrategyProfile(use_intelligence=True)
    res = build_targets(
        meta=meta,
        profile=p,
        yes_view=view(0.48, 0.52),
        no_view=view(0.48, 0.52),
        pos_yes=Position(meta.yes.token_id),
        pos_no=Position(meta.no.token_id),
        est=_est(),
        regime_machine=RegimeMachine(),
        now=1.0,
        micro=0.50,
        intel=DecisionFramework(),
        n_trades_last_hour=40,
        seconds_since_last_trade=1.0,
    )
    assert res is not None
    d = res.attribution.as_dict()
    assert "fair_value" in d
    assert "intelligence_decision" in d
    assert "reason_codes" in d
