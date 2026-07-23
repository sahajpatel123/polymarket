"""Tests for the paper fill simulator."""

from __future__ import annotations

from polymaker.domain import OpenOrder, Side
from polymaker.paper.fill_sim import FillSimulator


def _order(oid: str, token: str, side: Side, price: float, size: float) -> OpenOrder:
    return OpenOrder(oid, token, side, price, size)


def test_buy_order_filled_by_sell_aggressor() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert len(fills) == 1
    assert fills[0].side is Side.BUY
    assert fills[0].price == 0.50
    assert fills[0].size == 100
    assert fills[0].is_maker is True
    assert sim.all_orders() == []


def test_sell_order_filled_by_buy_aggressor() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.SELL, 0.52, 50))
    fills = sim.match("tok", Side.BUY, price=0.53, size=50, ts=1.0)
    assert len(fills) == 1
    assert fills[0].side is Side.SELL
    assert fills[0].price == 0.52
    assert fills[0].size == 50


def test_partial_fill_reduces_size() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=30, ts=1.0)
    assert len(fills) == 1
    assert fills[0].size == 30
    remaining = sim.all_orders()
    assert len(remaining) == 1
    assert remaining[0].size == 70


def test_no_fill_when_aggressor_does_not_cross() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    # SELL aggressor at 0.51 doesn't hit our 0.50 bid
    fills = sim.match("tok", Side.SELL, price=0.51, size=100, ts=1.0)
    assert fills == []
    assert len(sim.all_orders()) == 1


def test_pro_rata_fill_across_same_price() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 60))
    sim.place(_order("o2", "tok", Side.BUY, 0.50, 40))
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert len(fills) == 2
    assert sum(f.size for f in fills) == 100
    assert sim.all_orders() == []


def test_price_priority_best_first() -> None:
    sim = FillSimulator()
    # Two BUY orders at different prices; aggressor hits highest first
    sim.place(_order("o1", "tok", Side.BUY, 0.48, 100))
    sim.place(_order("o2", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert len(fills) == 1
    assert fills[0].price == 0.50  # highest bid filled first


def test_cancel_removes_order() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    sim.cancel("o1")
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert fills == []


def test_different_token_no_match() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tokA", Side.BUY, 0.50, 100))
    fills = sim.match("tokB", Side.SELL, price=0.49, size=100, ts=1.0)
    assert fills == []


def test_zero_size_trade_no_fill() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=0, ts=1.0)
    assert fills == []


def test_orders_for_returns_correct_token() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tokA", Side.BUY, 0.50, 100))
    sim.place(_order("o2", "tokB", Side.SELL, 0.52, 50))
    a_orders = sim.orders_for("tokA")
    assert len(a_orders) == 1
    assert a_orders[0].token_id == "tokA"


def test_clear_removes_all() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    sim.place(_order("o2", "tok", Side.SELL, 0.52, 50))
    sim.clear()
    assert sim.all_orders() == []
