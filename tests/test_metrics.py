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
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
        {"ts": t0 + 500, "event": "cancel", "condition_id": "c1", "token_id": "yes",
         "side": "BUY", "price": 0.45, "size": 50,
         "inventory_yes": 80, "inventory_no": 0, "inventory_net": 80},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze(path)
    assert rep.n_quote == 2
    assert rep.n_cancel == 1
    assert rep.n_fill == 1
    # BUY at 0.48 vs mid 0.50 → +0.02 * 100 = 2.0
    assert abs(rep.realized_spread_usdc - 2.0) < 1e-9
    assert rep.markout_n["30s"] == 1
    assert abs(rep.markout["30s"] - (-0.02)) < 1e-9  # 0.48 - 0.50
    assert abs(rep.markout["120s"] - (-0.03)) < 1e-9
    assert abs(rep.markout["300s"] - (-0.04)) < 1e-9
    assert rep.inventory_drift_abs_peak == 100.0
    assert rep.inventory_net_end["c1"] == 80.0
    # in-band from t0+1 to t0+400 = 399s; daily 86.4 → 86.4 * 399/86400
    assert abs(rep.reward_accrual_usdc["c1"] - 86.4 * 399 / 86400) < 1e-6
    assert rep.rebate_pool_daily_usdc["c1"] == 12.0


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
