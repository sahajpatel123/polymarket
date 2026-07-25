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


def test_pipeline_intel_skip_dead_tape(meta) -> None:
    p = StrategyProfile(use_intelligence=True, intelligence_mode="full")
    from dataclasses import replace

    m = replace(meta, rewards_daily_rate=0.0)
    fw = DecisionFramework()
    res = build_targets(
        meta=m,
        profile=p,
        yes_view=view(0.48, 0.52),
        no_view=view(0.48, 0.52),
        pos_yes=Position(m.yes.token_id),
        pos_no=Position(m.no.token_id),
        est=_est(),
        regime_machine=RegimeMachine(),
        now=1.0,
        micro=0.50,
        intel=fw,
        n_trades_last_hour=0,
        seconds_since_last_trade=0.0,
    )
    assert res is not None
    assert res.attribution.intelligence_decision == "SKIP"
    # No entry quotes when skipped (exits only if inventory)
    buys = [q for q in res.targets.quotes if q.side.value == "BUY"]
    assert buys == []


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
