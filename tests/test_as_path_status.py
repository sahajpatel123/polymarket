"""Tests for as_path_status aggregation."""

from __future__ import annotations

from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.as_path_status import assess_as_path


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xabc",
        question="t",
        slug="t-slug",
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


def test_as_path_blocked_when_at_touch_only():
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
    st = assess_as_path(rows, _meta(), journal="j.jsonl")
    assert st.ready is False
    assert st.n_through == 0
    assert "no_through_price_sells" in st.blockers


def test_as_path_ready_when_through_price_exists():
    rows = [
        {
            "kind": "book",
            "ts": 1.0,
            "data": {
                "asset_id": "yes-t",
                "market": "0xabc",
                "bids": [{"price": "0.50", "size": "50"}],
                "asks": [{"price": "0.52", "size": "50"}],
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
                "price": "0.49",
                "size": "10",
                "timestamp": "2000",
            },
        },
    ]
    st = assess_as_path(rows, _meta(), journal="j.jsonl")
    assert st.n_through == 1
    assert st.ready is True
    assert st.conservative_join_viable is True
