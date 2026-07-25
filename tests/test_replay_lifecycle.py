"""Replay order lifecycle integrity — Priority 0 trust gates."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import OpenOrder, Side
from polymaker.paper.fill_sim import FillSimulator
from polymaker.replay import (
    ReplayState,
    apply_journal_event,
    assert_order_sync,
    run_replay,
)


def _order(oid: str, token: str, side: Side, price: float, size: float) -> OpenOrder:
    return OpenOrder(oid, token, side, price, size)


def test_cancelled_order_never_fills() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    sim.cancel("o1")
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert fills == []


def test_full_fill_removes_from_sim() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=100, ts=1.0)
    assert len(fills) == 1
    assert fills[0].order_id == "o1"
    assert sim.order_ids() == set()


def test_partial_fill_updates_remaining() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 100))
    fills = sim.match("tok", Side.SELL, price=0.49, size=30, ts=1.0)
    assert fills[0].size == 30
    assert sim.remaining("o1") == 70


def test_trade_cannot_overfill_aggressor_size() -> None:
    sim = FillSimulator()
    sim.place(_order("o1", "tok", Side.BUY, 0.50, 500))
    fills = sim.match("tok", Side.SELL, price=0.49, size=40, ts=1.0)
    assert sum(f.size for f in fills) == 40
    assert sim.remaining("o1") == 460


def test_duplicate_trade_print_no_double_fill_in_replay(tmp_path: Path, meta) -> None:
    """Identical last_trade_price rows must not double-fill."""
    t0 = 1_700_000_000.0
    yes, no = meta.yes.token_id, meta.no.token_id
    rows = [
        {
            "ts": t0,
            "kind": "book",
            "data": {
                "market": meta.condition_id,
                "asset_id": yes,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int(t0 * 1000)),
                "tick_size": "0.01",
            },
        },
        {
            "ts": t0 + 0.1,
            "kind": "book",
            "data": {
                "market": meta.condition_id,
                "asset_id": no,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int((t0 + 0.1) * 1000)),
                "tick_size": "0.01",
            },
        },
    ]
    journal = tmp_path / "j.jsonl"
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    # Manual state: place, then two identical trade prints
    st = ReplayState(meta=meta, profile=StrategyProfile())
    for row in rows:
        apply_journal_event(st, row)
    o = OpenOrder("manual-1", yes, Side.BUY, 0.50, 100)
    st.live[o.order_id] = o
    st.fill_sim.place(o)
    trade = {
        "ts": t0 + 1.0,
        "kind": "last_trade_price",
        "data": {
            "market": meta.condition_id,
            "asset_id": yes,
            "price": "0.49",
            "size": "100",
            "side": "SELL",
            "timestamp": str(int((t0 + 1.0) * 1000)),
        },
    }
    apply_journal_event(st, trade)
    n1 = st.n_fill
    apply_journal_event(st, trade)  # duplicate
    assert st.n_fill == n1  # no double fill


def test_cancel_then_trade_no_fill_in_replay_sync(meta) -> None:
    st = ReplayState(meta=meta, profile=StrategyProfile())
    yes = meta.yes.token_id
    o = OpenOrder("c1", yes, Side.BUY, 0.50, 100)
    st.live[o.order_id] = o
    st.fill_sim.place(o)
    st.live.pop("c1")
    st.fill_sim.cancel("c1")
    st.cancelled_ids.add("c1")
    assert assert_order_sync(st) == []
    fills = st.fill_sim.match(yes, Side.SELL, 0.49, 100, 1.0)
    assert fills == []


def test_replay_end_zero_divergence(tmp_path: Path, meta) -> None:
    t0 = 1_700_000_000.0
    yes, no = meta.yes.token_id, meta.no.token_id
    rows = []
    for i in range(50):
        ts = t0 + i * 0.5
        mid = 0.50 + 0.01 * ((i % 5) - 2) * 0.1
        bb, ba = round(mid - 0.02, 2), round(mid + 0.02, 2)
        rows.append({
            "ts": ts,
            "kind": "book",
            "data": {
                "market": meta.condition_id,
                "asset_id": yes,
                "bids": [{"price": f"{bb:.2f}", "size": "500"}],
                "asks": [{"price": f"{ba:.2f}", "size": "500"}],
                "timestamp": str(int(ts * 1000)),
                "tick_size": "0.01",
            },
        })
        if i == 0:
            rows.append({
                "ts": ts + 0.01,
                "kind": "book",
                "data": {
                    "market": meta.condition_id,
                    "asset_id": no,
                    "bids": [{"price": "0.48", "size": "500"}],
                    "asks": [{"price": "0.52", "size": "500"}],
                    "timestamp": str(int((ts + 0.01) * 1000)),
                    "tick_size": "0.01",
                },
            })
        if i % 3 == 0:
            rows.append({
                "ts": ts + 0.1,
                "kind": "last_trade_price",
                "data": {
                    "market": meta.condition_id,
                    "asset_id": yes,
                    "price": f"{bb:.2f}",
                    "size": "25",
                    "side": "SELL" if i % 2 == 0 else "BUY",
                    "timestamp": str(int((ts + 0.1) * 1000)),
                },
            })
    journal = tmp_path / "long.jsonl"
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    metrics = tmp_path / "m.jsonl"
    r = run_replay(journal, meta, StrategyProfile(), metrics, strict_sync=True)
    assert r.state_divergence_events == 0
    assert r.fills_after_cancel == 0
    assert r.overfills == 0


def test_replay_determinism_byte_identical_counts(tmp_path: Path, meta) -> None:
    t0 = 1_700_000_000.0
    yes, no = meta.yes.token_id, meta.no.token_id
    rows = [
        {
            "ts": t0,
            "kind": "book",
            "data": {
                "market": meta.condition_id,
                "asset_id": yes,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int(t0 * 1000)),
                "tick_size": "0.01",
            },
        },
        {
            "ts": t0 + 0.1,
            "kind": "book",
            "data": {
                "market": meta.condition_id,
                "asset_id": no,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int((t0 + 0.1) * 1000)),
                "tick_size": "0.01",
            },
        },
        {
            "ts": t0 + 1.0,
            "kind": "last_trade_price",
            "data": {
                "market": meta.condition_id,
                "asset_id": yes,
                "price": "0.50",
                "size": "25",
                "side": "BUY",
                "timestamp": str(int((t0 + 1.0) * 1000)),
            },
        },
    ]
    journal = tmp_path / "j.jsonl"
    journal.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    r1 = run_replay(journal, meta, StrategyProfile(), tmp_path / "m1.jsonl")
    r2 = run_replay(journal, meta, StrategyProfile(), tmp_path / "m2.jsonl")
    assert (r1.n_quote, r1.n_fill, r1.n_cancel, r1.n_mark) == (
        r2.n_quote, r2.n_fill, r2.n_cancel, r2.n_mark,
    )
    assert r1.final_equity == r2.final_equity
