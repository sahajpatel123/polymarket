"""Tests for the realistic fill simulator and multi-horizon markout."""

from __future__ import annotations

from polymaker.domain import OpenOrder, OrderState, Side
from polymaker.paper.realistic_fill_sim import RealisticFillSimulator
from polymaker.strategy.estimators import MultiHorizonMarkout
from polymaker.strategy.estimators import Side as EstSide


def _live_order(oid: str, token: str, side: Side, price: float, size: float) -> OpenOrder:
    return OpenOrder(oid, token, side, price, size, OrderState.LIVE)


def test_realistic_fill_simulator_buy_order_filled() -> None:
    """BUY order filled by SELL aggressor at lower price."""
    sim = RealisticFillSimulator(latency_s=0.0)
    sim.place(_live_order("o1", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert len(fills) == 1
    assert fills[0].side is Side.BUY
    assert fills[0].size == 100


def test_realistic_fill_simulator_queue_position() -> None:
    """Orders with size ahead in queue don't get filled."""
    sim = RealisticFillSimulator(latency_s=0.0)
    sim.place(_live_order("o1", "tok", Side.BUY, 0.50, 100))
    # Small trade: not enough to reach us through the queue
    fills = sim.match("tok", Side.SELL, price=0.49, size=10, ts=1.0)
    # With 50% queue estimate, 10 shares isn't enough to clear the queue ahead
    assert len(fills) == 0
    assert sim.stats["n_queue_ahead_fills"] >= 1


def test_realistic_fill_simulator_latency() -> None:
    """Orders within latency window are not matched."""
    sim = RealisticFillSimulator(latency_s=1.0)
    sim.place(_live_order("o1", "tok", Side.BUY, 0.50, 100))
    # Immediate trade: should be filtered by latency
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert len(fills) == 0


def test_realistic_fill_simulator_stats() -> None:
    """Fill simulator tracks statistics."""
    sim = RealisticFillSimulator(latency_s=0.0)
    sim.place(_live_order("o1", "tok", Side.BUY, 0.50, 100))
    sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    stats = sim.stats
    assert "n_partial_fills" in stats
    assert "n_queue_ahead_fills" in stats
    assert stats["n_partial_fills"] >= 1


def test_multi_horizon_markout_basic() -> None:
    """Multi-horizon markout tracks multiple horizons."""
    mh = MultiHorizonMarkout(horizons_s=(30.0, 120.0, 300.0))
    mh.record_fill(EstSide.BUY, 0.50, 1.0)
    # Advance time past first horizon
    mh.evaluate(0.52, 31.0)  # price went up 0.02
    assert mh.short_term_toxicity == 0.0  # positive markout = good
    assert mh.markout > 0


def test_multi_horizon_markout_toxicity() -> None:
    """Negative markout produces toxicity."""
    mh = MultiHorizonMarkout(horizons_s=(30.0, 120.0, 300.0))
    mh.record_fill(EstSide.BUY, 0.50, 1.0)
    # Price went DOWN (bad for BUY)
    mh.evaluate(0.48, 31.0)
    assert mh.short_term_toxicity > 0  # negative markout = toxic
    assert mh.toxicity > 0


def test_multi_horizon_markout_weighted() -> None:
    """Combined toxicity is a weighted average across horizons."""
    mh = MultiHorizonMarkout(
        horizons_s=(30.0, 120.0, 300.0),
        weights=(0.5, 0.3, 0.2),
    )
    mh.record_fill(EstSide.BUY, 0.50, 1.0)
    # Resolve all three horizons with same adverse move
    mh.evaluate(0.48, 31.0)
    mh.evaluate(0.48, 121.0)
    mh.evaluate(0.48, 301.0)
    # All three should be equally toxic
    per_horizon = mh.per_horizon_markout()
    assert len(per_horizon) == 3
    # Combined should be negative (toxic)
    assert mh.markout < 0
    assert mh.toxicity > 0


def test_multi_horizon_markout_short_vs_long() -> None:
    """Short-term toxicity is more recent than long-term."""
    mh = MultiHorizonMarkout(horizons_s=(30.0, 300.0))
    mh.record_fill(EstSide.BUY, 0.50, 1.0)
    # Short-term: good (price up)
    mh.evaluate(0.52, 31.0)
    # Long-term: bad (price down)
    mh.evaluate(0.48, 301.0)
    assert mh.short_term_toxicity == 0.0  # short-term was good
    assert mh.long_term_toxicity > 0  # long-term was bad
