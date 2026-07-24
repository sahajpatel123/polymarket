"""Unit tests for metrics logger + analyze (Tier-1 paper metrics)."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.metrics import MetricsLogger, inventory_fields
from polymaker.metrics.analyze import analyze


def test_inventory_fields() -> None:
    assert inventory_fields(10.0, 3.0)["inventory_net"] == 7.0


def test_metrics_logger_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    ml = MetricsLogger(path)
    ml.emit("quote", condition_id="c1", token_id="t", side="BUY", price=0.4, size=10.0,
            **inventory_fields(0, 0))
    ml.emit("cancel", condition_id="c1", token_id="t", side="BUY", price=0.4, size=10.0,
            **inventory_fields(0, 0))
    ml.close()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "quote"
    assert json.loads(lines[1])["event"] == "cancel"


def test_metrics_logger_flushes_quotes_immediately(tmp_path: Path) -> None:
    """Quotes must hit disk without waiting for the mark batch threshold."""
    path = tmp_path / "m.jsonl"
    ml = MetricsLogger(path)
    ml.emit("mark", condition_id="c1", fv=0.5)
    assert path.read_text().strip() == ""  # marks stay buffered
    ml.emit("quote", condition_id="c1", token_id="t", side="BUY", price=0.4, size=1.0,
            mid=0.41, fv_yes=0.41, **inventory_fields(0, 0))
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2  # mark + quote flushed together
    assert json.loads(lines[1])["event"] == "quote"
    assert json.loads(lines[1])["fv_yes"] == 0.41
    ml.close()


def test_analyze_computes_spread_markout_inventory_reward(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    t0 = 1_000_000.0
    rows = [
        {"ts": t0, "event": "market_meta", "condition_id": "c1",
         "rewards_daily_rate": 86.4, "rebate_potential_daily": 12.0},
        {"ts": t0, "event": "mark", "condition_id": "c1", "fv": 0.50,
         "inventory_yes": 0, "inventory_no": 0, "inventory_net": 0},
        {"ts": t0 + 1, "event": "quote", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.48, "size": 100, "in_reward_band": True,
         "order_id": "o1",
         "inventory_yes": 0, "inventory_no": 0, "inventory_net": 0},
        {"ts": t0 + 10, "event": "fill", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.48, "size": 100, "mid": 0.50,
         "inventory_yes": 100, "inventory_no": 0, "inventory_net": 100},
        # adverse: price falls 0.02 after fill — bad for BUY
        {"ts": t0 + 40, "event": "mark", "condition_id": "c1", "fv": 0.48,
         "inventory_yes": 100, "inventory_no": 0, "inventory_net": 100},
        {"ts": t0 + 130, "event": "mark", "condition_id": "c1", "fv": 0.47,
         "inventory_yes": 100, "inventory_no": 0, "inventory_net": 100},
        {"ts": t0 + 310, "event": "mark", "condition_id": "c1", "fv": 0.46,
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
        {"ts": t0 + 400, "event": "quote", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.45, "size": 50, "in_reward_band": True,
         "order_id": "o2",
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
        # cancel both resting orders → accrual stops
        {"ts": t0 + 400, "event": "cancel", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.48, "size": 100, "order_id": "o1",
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
        {"ts": t0 + 400, "event": "cancel", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.45, "size": 50, "order_id": "o2",
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
        # post-cancel marks must NOT earn reward rent
        {"ts": t0 + 4000, "event": "mark", "condition_id": "c1", "fv": 0.46,
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze(path)
    assert rep.n_quote == 2
    assert rep.n_cancel == 2
    assert rep.n_fill == 1
    # BUY at 0.48 vs mid 0.50 → +0.02 * 100 = 2.0
    assert abs(rep.realized_spread_usdc - 2.0) < 1e-9
    assert rep.markout_n["30s"] == 1
    assert abs(rep.markout["30s"] - (-0.02)) < 1e-9  # 0.48 - 0.50
    assert abs(rep.markout["120s"] - (-0.03)) < 1e-9
    assert abs(rep.markout["300s"] - (-0.04)) < 1e-9
    assert rep.inventory_drift_abs_peak == 100.0
    assert rep.inventory_net_end["c1"] == 80.0
    # in-band while o1/o2 live: t0+1 → t0+400 = 399s (post-cancel hour ignored)
    assert abs(rep.in_band_seconds["c1"] - 399.0) < 1e-6
    assert abs(rep.reward_accrual_usdc["c1"] - 86.4 * 399 / 86400) < 1e-6
    assert rep.rebate_pool_daily_usdc["c1"] == 12.0


def test_cancel_stops_reward_accrual(tmp_path: Path) -> None:
    """A single in-band quote then cancel must not rent the rest of the window."""
    path = tmp_path / "m.jsonl"
    t0 = 2_000_000.0
    daily = 240.0  # $240/day pool
    rows = [
        {"ts": t0, "event": "market_meta", "condition_id": "c1", "rewards_daily_rate": daily},
        {"ts": t0, "event": "quote", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.5, "size": 10, "in_reward_band": True, "order_id": "o1"},
        {"ts": t0 + 60, "event": "cancel", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.5, "size": 10, "order_id": "o1"},
        # 1h of marks after cancel — must not count
        {"ts": t0 + 60 + 3600, "event": "mark", "condition_id": "c1", "fv": 0.5},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze(path)
    # only 60s in-band → 240 * 60/86400 = $0.1667, NOT $10 from 1h post-cancel
    assert abs(rep.in_band_seconds["c1"] - 60.0) < 1e-6
    assert abs(rep.reward_accrual_usdc["c1"] - daily * 60 / 86400) < 1e-6
    assert rep.reward_accrual_usdc["c1"] < 1.0  # nowhere near $10


def test_quote_quality_counters(tmp_path: Path) -> None:
    """Dust and OOB counters: prove the band_lo filter is working."""
    path = tmp_path / "m.jsonl"
    rows = [
        {"ts": 1.0, "event": "market_meta", "condition_id": "c1",
         "rewards_daily_rate": 100.0, "rewards_max_spread": 5.0},
        # In-band quote: counts as in_band
        {"ts": 1.0, "event": "quote", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.5, "size": 10, "in_reward_band": True, "mid": 0.5,
         "order_id": "o1"},
        # Dust quote (sub-cent): counts as dust
        {"ts": 2.0, "event": "quote", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.001, "size": 10, "in_reward_band": True, "mid": 0.5,
         "order_id": "o2"},
        # OOB quote: not in band, counts as oob
        {"ts": 3.0, "event": "quote", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.30, "size": 10, "in_reward_band": False, "mid": 0.5,
         "order_id": "o3"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze(path)
    assert rep.n_quote == 3
    # 2 in-band: the normal quote and the dust quote (both in_reward_band=True).
    # Dust is a SEPARATE counter; an in-band dust still counts as dust.
    assert rep.n_in_band_quotes == 2
    assert rep.n_dust_quotes == 1
    # OOB: |0.30 - 0.5| = 0.20 > 5c band (0.05)
    assert rep.n_oob_quotes == 1


def test_analyze_runtime_from_journal_timestamps(tmp_path: Path) -> None:
    """Runtime must be computed from journal timestamps, not wall-clock.

    Regression: market_meta used time.time() which made max-min span span
    hours/days for historical tapes, inflating daily_return_pct by 7x+.
    """
    # This test validates the same concept but at the replay level: the
    # market_meta event must use the journal's first timestamp, not time.time().
    # We import run_replay to verify by checking the metrics output.
    from polymaker.config import StrategyProfile
    from polymaker.domain import MarketMeta, TokenMeta
    from polymaker.replay import run_replay

    path = tmp_path / "m.jsonl"
    journal = tmp_path / "j.jsonl"
    # 3-hour journal window from 2026-07-22
    j_rows = [
        {"ts": 1_784_703_581.0, "kind": "book",
         "data": {"market": "0xtest", "asset_id": "yes-tok",
                  "bids": [{"price": "0.49", "size": "100"}],
                  "asks": [{"price": "0.51", "size": "100"}],
                  "timestamp": "1784703581000", "tick_size": "0.01"}},
        {"ts": 1_784_714_381.0, "kind": "book",
         "data": {"market": "0xtest", "asset_id": "yes-tok",
                  "bids": [{"price": "0.49", "size": "100"}],
                  "asks": [{"price": "0.51", "size": "100"}],
                  "timestamp": "1784714381000", "tick_size": "0.01"}},
    ]
    journal.write_text("\n".join(json.dumps(r) for r in j_rows) + "\n")
    meta = MarketMeta(
        condition_id="0xtest", question="test?", slug="t",
        tokens=(TokenMeta("yes-tok", "Yes"), TokenMeta("no-tok", "No")),
        tick_size=0.01, neg_risk=False, min_order_size=5.0,
        rewards_min_size=5.0, rewards_max_spread=3.0,
        rewards_daily_rate=0.0, maker_fee_bps=0, taker_fee_bps=400,
        fees_enabled=True, end_date_iso=None, event_id=None,
        rebate_rate=0.0,
    )
    run_replay(journal, meta, StrategyProfile(), path)
    # Check the market_meta event uses journal timestamp, not wall-clock
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    market_meta = next(e for e in events if e["event"] == "market_meta")
    # Journal starts at 1784703581; market_meta ts must be close to that,
    # not ~2 years in the future (wall-clock 2026-07-26 = ~1784_800_000)
    assert abs(market_meta["ts"] - 1_784_703_581.0) < 1.0, (
        f"market_meta ts {market_meta['ts']} should match journal start, "
        f"not wall-clock (would be ~2 years off)"
    )


async def test_engine_paper_recompute_emits_metrics(tmp_path: Path, meta) -> None:
    """Integration: paper recompute writes quote+mark into metrics log."""
    import time

    from polymaker.config import Config, PathsConfig, StrategyProfile
    from polymaker.domain import Side
    from polymaker.engine import Engine
    from polymaker.strategy.regime import RegimeMachine

    cfg = Config(paths=PathsConfig(db=str(tmp_path / "state.db"),
                                   journal_dir=str(tmp_path / "j"),
                                   log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    eng = Engine(cfg, paper=True)
    cid = meta.condition_id
    eng.metas[cid] = meta
    eng.profiles[cid] = StrategyProfile()
    eng.est[cid] = Engine._make_estimators(eng.profiles[cid])
    eng.regime_m[cid] = RegimeMachine()
    eng._dirty[cid] = __import__("asyncio").Event()
    eng._locks[cid] = __import__("asyncio").Lock()
    for tok in (meta.yes.token_id, meta.no.token_id):
        eng._token_cid[tok] = cid
    eng.md.set_markets([(cid, [meta.yes.token_id, meta.no.token_id])])
    eng._running = True
    now = time.time()
    eng.md.book(meta.yes.token_id).apply_snapshot(
        bids=[(0.48, 500), (0.49, 500)], asks=[(0.51, 500), (0.52, 500)], ts=now
    )
    eng.md.book(meta.no.token_id).apply_snapshot(
        bids=[(0.48, 500), (0.49, 500)], asks=[(0.51, 500), (0.52, 500)], ts=now
    )

    await eng._recompute(cid)
    eng.metrics.close()
    path = tmp_path / "l" / "metrics-paper.jsonl"
    assert path.exists()
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    kinds = {e["event"] for e in events}
    assert "mark" in kinds
    assert "quote" in kinds
    quotes = [e for e in events if e["event"] == "quote"]
    assert quotes
    assert all("inventory_net" in q for q in quotes)
    assert all(q["side"] in (Side.BUY.value, Side.SELL.value) for q in quotes)
    eng.state.close()
    eng.catalog.close()
