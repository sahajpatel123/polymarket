"""The daily-loss stop must remove exposure, not just stop new quotes.

Observed on a $100 paper book with a $10 daily cap: the stop engaged at -$64,
then four further BUY fills worth $67 landed over the next 40 minutes. Cause:
emptying target quotes only stops *new* orders. A market that goes blind on a
WS drop, or is paused by the oversight loop, stops being requoted at all, so its
cancel path never runs and its resting BUY orders remain fillable.
"""

from __future__ import annotations

import asyncio

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import OpenOrder, Side
from polymaker.engine import Engine
from polymaker.strategy.regime import RegimeMachine


def _engine(tmp_path, meta) -> Engine:
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
    eng._dirty[cid] = asyncio.Event()
    eng._locks[cid] = asyncio.Lock()
    for tok in (meta.yes.token_id, meta.no.token_id):
        eng._token_cid[tok] = cid
    eng._running = True
    return eng


def _rest(eng: Engine, meta, n: int = 3) -> list[str]:
    """Put n resting BUY orders in both the state store and the paper sim."""
    ids = []
    for i in range(n):
        o = OpenOrder(f"paper-{i}", meta.yes.token_id, Side.BUY, 0.30, 50.0)
        eng.state.upsert_order(o)
        eng._fill_sim.place(o)
        ids.append(o.order_id)
    return ids


def test_daily_stop_cancels_resting_orders(tmp_path, meta) -> None:
    eng = _engine(tmp_path, meta)
    ids = _rest(eng, meta)
    assert len(eng.state.orders) == 3
    assert len(eng._fill_sim.all_orders()) == 3

    asyncio.run(eng._flatten_on_daily_stop())

    assert eng.state.orders == {}, "resting orders survived the daily stop"
    assert eng._fill_sim.all_orders() == [], (
        "paper simulator still holds fillable orders after the daily stop — "
        "exposure keeps growing after the cap is breached"
    )
    for oid in ids:
        assert oid not in eng.state.orders
    eng.state.close()


def test_flattened_orders_can_no_longer_fill(tmp_path, meta) -> None:
    """The decisive property: no fill can occur after the stop."""
    eng = _engine(tmp_path, meta)
    _rest(eng, meta)
    # sanity: before the stop, a crossing print WOULD fill
    pre = eng._fill_sim.match(meta.yes.token_id, Side.SELL, price=0.29,
                              size=10.0, ts=1.0)
    assert pre, "expected the resting buy to be fillable before the stop"

    asyncio.run(eng._flatten_on_daily_stop())

    post = eng._fill_sim.match(meta.yes.token_id, Side.SELL, price=0.29,
                               size=1000.0, ts=2.0)
    assert post == [], "a fill landed after the daily-loss stop engaged"
    eng.state.close()


def test_daily_stop_flatten_is_idempotent(tmp_path, meta) -> None:
    """Runs once per breach; repeat calls must not raise or re-cancel."""
    eng = _engine(tmp_path, meta)
    _rest(eng, meta)
    asyncio.run(eng._flatten_on_daily_stop())
    assert eng._daily_stop_flattened is True
    # second call is a no-op even with new orders resting
    o = OpenOrder("paper-late", meta.yes.token_id, Side.BUY, 0.30, 50.0)
    eng.state.upsert_order(o)
    asyncio.run(eng._flatten_on_daily_stop())
    assert "paper-late" in eng.state.orders, (
        "one-shot guard should prevent repeated mass cancels"
    )
    eng.state.close()
