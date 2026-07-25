"""Equity ledger exact accounting invariants."""

from __future__ import annotations

from polymaker.accounting import EquityLedger
from polymaker.domain import Fill, Side


def test_buy_sell_roundtrip_cash_and_equity() -> None:
    led = EquityLedger()
    led.reset_day()
    led.apply_fill(Fill("yes", Side.BUY, 0.40, 100, "t1", order_id="o1"))
    assert abs(led.cash - (-40.0)) < 1e-9
    assert led.positions["yes"] == 100
    led.update_mark("yes", 0.45)
    eq1 = led.equity()
    assert abs(eq1 - (-40.0 + 45.0)) < 1e-9
    led.apply_fill(Fill("yes", Side.SELL, 0.45, 100, "t2", order_id="o1"))
    assert led.positions.get("yes", 0) == 0
    # cash: -40 + 45 = +5; inv = 0 → equity 5
    assert abs(led.equity() - 5.0) < 1e-9
    assert abs(led.realized_spread - 5.0) < 1e-9
    led.assert_invariants()


def test_equity_equals_cash_plus_inventory() -> None:
    led = EquityLedger()
    led.apply_fill(Fill("a", Side.BUY, 0.5, 20, "f1"))
    led.update_mark("a", 0.6)
    assert abs(led.equity() - (led.cash + led.inventory_value())) < 1e-9
    led.assert_invariants()


def test_fees_reduce_cash() -> None:
    led = EquityLedger()
    led.apply_fill(Fill("a", Side.BUY, 0.5, 10, "f1"))
    before = led.equity()
    led.add_fee(0.5)
    assert abs(led.equity() - (before - 0.5)) < 1e-9
