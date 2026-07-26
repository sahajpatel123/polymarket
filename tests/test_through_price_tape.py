"""Unit tests for through-price tape classification."""

from __future__ import annotations

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.through_price_tape import measure_through_price_tape


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xabc",
        question="t",
        slug="t",
        tokens=(TokenMeta("yes-t", "Yes"), TokenMeta("no-t", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=5.5,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def test_through_vs_at_touch_classification():
    rows = [
        {
            "kind": "book",
            "ts": 1.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.52", "size": "100"}],
                "timestamp": "1000",
            },
        },
        {
            "kind": "last_trade_price",
            "ts": 2.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "side": "SELL",
                "price": "0.50",
                "size": "10",
                "timestamp": "2000",
            },
        },
        {
            "kind": "last_trade_price",
            "ts": 3.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "side": "SELL",
                "price": "0.49",
                "size": "5",
                "timestamp": "3000",
            },
        },
        {
            "kind": "last_trade_price",
            "ts": 4.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "side": "SELL",
                "price": "0.51",
                "size": "5",
                "timestamp": "4000",
            },
        },
    ]
    r = measure_through_price_tape(rows, _meta())
    assert r.n_sell == 3
    assert r.n_at_touch == 1
    assert r.n_through == 1
    assert r.n_above_touch == 1
    assert r.conservative_join_viable is True


def test_at_touch_only_not_viable():
    rows = [
        {
            "kind": "book",
            "ts": 1.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "bids": [{"price": "0.40", "size": "50"}],
                "asks": [{"price": "0.42", "size": "50"}],
                "timestamp": "1000",
            },
        },
        {
            "kind": "last_trade_price",
            "ts": 2.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "side": "SELL",
                "price": "0.40",
                "size": "10",
                "timestamp": "2000",
            },
        },
    ]
    r = measure_through_price_tape(rows, _meta())
    assert r.n_through == 0
    assert r.n_at_touch == 1
    assert r.conservative_join_viable is False
    assert "at_touch_only" in r.reason
