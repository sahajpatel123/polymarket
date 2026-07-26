"""Tests for quote–trade gap diagnostic."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.quote_trade_gap import measure_quote_trade_gap


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xgap",
        question="gap",
        slug="gap",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=5.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def test_measure_quote_trade_gap_runs_on_books(tmp_path: Path) -> None:
    rows = []
    t0 = 1_000_000.0
    for i in range(30):
        rows.append(
            {
                "kind": "book",
                "ts": t0 + i,
                "data": {
                    "asset_id": "yes-token",
                    "bids": [{"price": "0.48", "size": "100"}],
                    "asks": [{"price": "0.52", "size": "100"}],
                    "timestamp": str(int((t0 + i) * 1000)),
                },
            }
        )
        rows.append(
            {
                "kind": "book",
                "ts": t0 + i + 0.1,
                "data": {
                    "asset_id": "no-token",
                    "bids": [{"price": "0.48", "size": "100"}],
                    "asks": [{"price": "0.52", "size": "100"}],
                    "timestamp": str(int((t0 + i) * 1000)),
                },
            }
        )
    # Trade above our typical bid — expect gap reason, not crash
    rows.append(
        {
            "kind": "last_trade_price",
            "ts": t0 + 40,
            "data": {
                "asset_id": "yes-token",
                "price": "0.55",
                "size": "10",
                "side": "BUY",
            },
        }
    )
    gap = measure_quote_trade_gap(rows, _meta(), StrategyProfile())
    assert gap.n_trades == 1
    assert gap.n_aggressor_buy == 1
    d = gap.as_dict()
    assert "reason" in d
    path = tmp_path / "g.json"
    path.write_text(json.dumps(d))
    assert path.exists()
