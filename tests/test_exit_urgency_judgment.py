"""Enter/skip + toxic defense + cut/exit with inventory on shared path."""

from __future__ import annotations

from polymaker.config import StrategyProfile
from polymaker.domain import Position, Regime, Side
from polymaker.intelligence import DecisionFramework, MarketFeatures
from polymaker.strategy.decision_pipeline import build_targets
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.quoting import QuoteInputs, construct_quotes
from polymaker.strategy.regime import RegimeMachine
from tests.conftest import view


def _est() -> MarketEstimators:
    return MarketEstimators(
        vol=VolEstimator(10, 600),
        flow=FlowEstimator(120),
        markout=MarkoutTracker(),
    )


def test_intel_skip_still_exits_inventory(meta, profile) -> None:
    held = Position(meta.yes.token_id, size=50.0, avg_price=0.48)
    tq = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        profile=profile, now=1.0, intel_skip=True,
        yes_exit_urgency=0.8,
    ))
    buys = [q for q in tq.quotes if q.side is Side.BUY]
    sells = [q for q in tq.quotes if q.side is Side.SELL and q.token_id == meta.yes.token_id]
    assert buys == []
    assert sells, "must still exit held YES when intel skips entries"


def test_higher_exit_urgency_lowers_sell_price(meta, profile) -> None:
    held = Position(meta.yes.token_id, size=50.0, avg_price=0.48)
    passive = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.0, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        profile=profile, now=1.0, yes_exit_urgency=0.0,
    ))
    urgent = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.QUIET, fv=0.5, vol_short=0.0, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=held, pos_no=Position(meta.no.token_id),
        profile=profile, now=1.0, yes_exit_urgency=1.0,
    ))
    def sell_px(tq):
        xs = [q.price for q in tq.quotes if q.side is Side.SELL and q.token_id == meta.yes.token_id]
        return min(xs) if xs else None
    pp, up = sell_px(passive), sell_px(urgent)
    assert pp is not None and up is not None
    assert up <= pp + 1e-12  # more urgency → more aggressive (lower) sell


def test_reduce_only_no_new_entries(meta, profile) -> None:
    tq = construct_quotes(QuoteInputs(
        meta=meta, regime=Regime.REDUCE_ONLY, fv=0.5, vol_short=0.01, toxicity=0.0,
        yes_view=view(0.49, 0.51), no_view=view(0.49, 0.51),
        pos_yes=Position(meta.yes.token_id, size=40.0, avg_price=0.5),
        pos_no=Position(meta.no.token_id),
        profile=profile, now=1.0,
    ))
    buys = [q for q in tq.quotes if q.side is Side.BUY]
    assert buys == []
    sells = [q for q in tq.quotes if q.side is Side.SELL]
    assert sells  # exit inventory


def test_pipeline_toxic_more_passive_than_quiet(meta) -> None:
    p = StrategyProfile(use_intelligence=True, intelligence_mode="full")
    fw = DecisionFramework()
    quiet = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=50, rewards_daily_rate=100.0,
        toxicity=0.0, flow_z=0.0, vol_ratio=1.0,
    )
    toxic = MarketFeatures(
        best_bid=0.49, best_ask=0.51, mid_price=0.5,
        n_trades_last_hour=50, rewards_daily_rate=100.0,
        toxicity=0.08, flow_z=0.0, vol_ratio=1.5,
    )
    fw.update_features("q", quiet)
    fw.update_microstructure("q", 0.49, 0.51, 100, 100, 1.0)
    rq = build_targets(
        meta=meta, profile=p, yes_view=view(0.48, 0.52), no_view=view(0.48, 0.52),
        pos_yes=Position(meta.yes.token_id), pos_no=Position(meta.no.token_id),
        est=_est(), regime_machine=RegimeMachine(), now=1.0, micro=0.5,
        intel=fw, n_trades_last_hour=50, seconds_since_last_trade=1.0,
    )
    fw.update_features("t", toxic)
    fw.update_microstructure("t", 0.49, 0.51, 100, 100, 2.0)
    rt = build_targets(
        meta=meta, profile=p, yes_view=view(0.48, 0.52), no_view=view(0.48, 0.52),
        pos_yes=Position(meta.yes.token_id), pos_no=Position(meta.no.token_id),
        est=_est(), regime_machine=RegimeMachine(), now=2.0, micro=0.5,
        intel=fw, n_trades_last_hour=50, seconds_since_last_trade=1.0,
    )
    assert rq is not None and rt is not None
    assert rt.attribution.size_multiplier <= rq.attribution.size_multiplier
    if rq.attribution.buy_band_frac is not None and rt.attribution.buy_band_frac is not None:
        assert rt.attribution.buy_band_frac <= rq.attribution.buy_band_frac
