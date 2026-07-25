"""≥100k-event (or multi-pass equivalent) replay lifecycle stress."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay import run_replay


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xlong",
        question="long tape",
        slug="long-tape",
        tokens=(TokenMeta("yes-long", "Yes"), TokenMeta("no-long", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=5.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-01-01T00:00:00Z",
        event_id="long",
    )


def _write_dense_journal(path: Path, n_events: int = 100_000) -> None:
    """Deterministic dense tape: book snaps + trades for yes/no."""
    t0 = 1_700_000_000.0
    yes, no = "yes-long", "no-long"
    rows: list[dict] = []
    # Seed books
    for i, tok in enumerate((yes, no)):
        rows.append({
            "ts": t0 + i * 0.001,
            "kind": "book",
            "data": {
                "market": "0xlong",
                "asset_id": tok,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.49", "size": "500"}],
                "asks": [{"price": "0.51", "size": "500"}, {"price": "0.52", "size": "500"}],
                "timestamp": str(int((t0 + i * 0.001) * 1000)),
                "tick_size": "0.01",
            },
        })
    # Remaining events alternate book/trade
    for i in range(n_events - 2):
        ts = t0 + 1.0 + i * 0.05
        if i % 4 == 0:
            mid = 0.50 + 0.01 * ((i % 11) - 5) * 0.1
            bb = round(max(0.01, mid - 0.02), 2)
            ba = round(min(0.99, mid + 0.02), 2)
            if bb >= ba:
                bb, ba = 0.48, 0.52
            rows.append({
                "ts": ts,
                "kind": "book",
                "data": {
                    "market": "0xlong",
                    "asset_id": yes if i % 8 < 4 else no,
                    "bids": [{"price": f"{bb:.2f}", "size": "400"}],
                    "asks": [{"price": f"{ba:.2f}", "size": "400"}],
                    "timestamp": str(int(ts * 1000)),
                    "tick_size": "0.01",
                },
            })
        else:
            side = "BUY" if i % 2 == 0 else "SELL"
            px = "0.49" if side == "SELL" else "0.51"
            rows.append({
                "ts": ts,
                "kind": "last_trade_price",
                "data": {
                    "market": "0xlong",
                    "asset_id": yes if i % 3 else no,
                    "price": px,
                    "size": "15",
                    "side": side,
                    "timestamp": str(int(ts * 1000)),
                },
            })
    path.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n")
    assert len(rows) >= n_events


def test_long_tape_zero_divergence_and_determinism(tmp_path: Path) -> None:
    journal = tmp_path / "long.jsonl"
    # 100k events is heavy; use 25k × ensure multi-pass equivalent by 4-seed... 
    # Plan says ≥100k OR multi-pass equivalent. Generate 100k for real stress.
    _write_dense_journal(journal, n_events=100_000)
    meta = _meta()
    profile = StrategyProfile(use_intelligence=False)
    m1 = tmp_path / "m1.jsonl"
    m2 = tmp_path / "m2.jsonl"
    r1 = run_replay(journal, meta, profile, m1, strict_sync=True, fill_mode="conservative")
    r2 = run_replay(journal, meta, profile, m2, strict_sync=True, fill_mode="conservative")
    assert r1.events_read >= 100_000
    assert r1.state_divergence_events == 0
    assert r1.fills_after_cancel == 0
    assert r1.overfills == 0
    assert (r1.n_quote, r1.n_fill, r1.n_cancel, r1.n_mark) == (
        r2.n_quote, r2.n_fill, r2.n_cancel, r2.n_mark,
    )
    assert r1.final_equity == r2.final_equity


def test_optimistic_fills_ge_conservative_on_same_tape(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    _write_dense_journal(journal, n_events=2_000)
    meta = _meta()
    profile = StrategyProfile()
    ro = run_replay(journal, meta, profile, tmp_path / "o.jsonl", fill_mode="optimistic")
    rc = run_replay(journal, meta, profile, tmp_path / "c.jsonl", fill_mode="conservative")
    assert ro.n_fill >= rc.n_fill
    assert ro.state_divergence_events == 0
    assert rc.state_divergence_events == 0
