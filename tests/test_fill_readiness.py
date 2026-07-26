"""Tests for fill-readiness gate."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.fill_readiness import assess_fill_readiness
from polymaker.replay.quant_edge import evaluate_quant_edge
from polymaker.replay.compare import profile_from_overrides


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xready",
        question="ready",
        slug="ready",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def _journal(path: Path, *, n_trades: int, n_pc: int = 20) -> None:
    rows = []
    t0 = 1_000_000.0
    for i in range(n_pc):
        rows.append(
            {
                "kind": "price_change",
                "ts": t0 + i,
                "data": {
                    "asset_id": "yes-token",
                    "changes": [{"price": "0.50", "size": "10", "side": "BUY"}],
                },
            }
        )
    for i in range(n_trades):
        rows.append(
            {
                "kind": "last_trade_price",
                "ts": t0 + n_pc + i,
                "data": {
                    "asset_id": "yes-token",
                    "price": "0.50",
                    "size": "5",
                    "side": "BUY",
                },
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_fill_readiness_fails_sparse_trades(tmp_path: Path) -> None:
    j = tmp_path / "sparse.jsonl"
    _journal(j, n_trades=5)
    r = assess_fill_readiness(j, _meta(), min_trades=50)
    assert r.as_ev_ready is False
    assert r.n_trades == 5
    assert "n_trades=" in r.reason


def test_fill_readiness_passes_dense_trades(tmp_path: Path) -> None:
    j = tmp_path / "dense.jsonl"
    _journal(j, n_trades=60)
    r = assess_fill_readiness(j, _meta(), min_trades=50)
    assert r.as_ev_ready is True
    assert r.n_trades == 60


def test_quant_edge_embeds_fill_readiness(tmp_path: Path) -> None:
    j = tmp_path / "j.jsonl"
    # Thin trade count → as_ev_ready false → not promotion_eligible
    rows = []
    t0 = 1_000_000.0
    for i in range(80):
        rows.append(
            {
                "kind": "book",
                "ts": t0 + i,
                "data": {
                    "asset_id": "yes-token",
                    "bids": [{"price": "0.49", "size": "100"}],
                    "asks": [{"price": "0.51", "size": "100"}],
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
                    "bids": [{"price": "0.49", "size": "100"}],
                    "asks": [{"price": "0.51", "size": "100"}],
                    "timestamp": str(int((t0 + i) * 1000)),
                },
            }
        )
    j.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    baseline = StrategyProfile()
    candidate = profile_from_overrides(baseline, {"use_advanced_quoting": True})
    result = evaluate_quant_edge(
        j,
        _meta(),
        baseline,
        candidate,
        tmp_path / "out",
        holdout_frac=0.3,
        n_chunks=3,
        min_trades_for_as=50,
    )
    d = result.as_dict()
    assert d["verdict"]["as_ev_ready"] is False
    assert d["verdict"]["promotion_eligible"] is False
    assert "fill_readiness" in d["verdict"]
