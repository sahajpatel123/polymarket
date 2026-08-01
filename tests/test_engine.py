"""Integration test: one full engine recompute cycle in paper mode (no network)."""

from __future__ import annotations

import asyncio
import time

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import Side
from polymaker.engine import Engine
from polymaker.strategy.regime import RegimeMachine


def _engine_with_market(tmp_path, meta) -> Engine:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "state.db"),
                                   journal_dir=str(tmp_path / "j"),
                                   log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    eng = Engine(cfg, paper=True)
    cid = meta.condition_id
    # inject one market directly, bypassing network resolution
    eng.metas[cid] = meta
    eng.profiles[cid] = StrategyProfile()
    eng.est[cid] = Engine._make_estimators(eng.profiles[cid])
    eng.regime_m[cid] = RegimeMachine()
    eng._dirty[cid] = asyncio.Event()
    eng._locks[cid] = asyncio.Lock()
    for tok in (meta.yes.token_id, meta.no.token_id):
        eng._token_cid[tok] = cid
    eng.md.set_markets([(cid, [meta.yes.token_id, meta.no.token_id])])
    eng._running = True
    return eng


def _feed_book(eng, meta):
    now = time.time()  # fresh ts so the ws_stale guard doesn't HALT the market
    yb = eng.md.book(meta.yes.token_id)
    yb.apply_snapshot(bids=[(0.48, 500), (0.49, 500)], asks=[(0.51, 500), (0.52, 500)], ts=now)
    nb = eng.md.book(meta.no.token_id)
    nb.apply_snapshot(bids=[(0.48, 500), (0.49, 500)], asks=[(0.51, 500), (0.52, 500)], ts=now)


async def test_recompute_places_two_sided_paper_quotes(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)

    yes_orders = eng.state.orders_for(meta.yes.token_id)
    no_orders = eng.state.orders_for(meta.no.token_id)
    assert yes_orders, "no YES quotes placed"
    assert no_orders, "no NO quotes placed"
    # entry quotes are BUYs on both tokens (the canonical two-sided quote)
    assert all(o.side is Side.BUY for o in yes_orders)
    assert all(o.side is Side.BUY for o in no_orders)
    eng.state.close()
    eng.catalog.close()


async def test_recompute_is_idempotent_within_tolerance(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    _feed_book(eng, meta)
    await eng._recompute(meta.condition_id)
    n_after_first = len(eng.state.orders)
    # same book -> reconcile should be a no-op, order count unchanged
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == n_after_first
    eng.state.close()
    eng.catalog.close()


async def test_recompute_skips_when_book_empty(tmp_path, meta):
    eng = _engine_with_market(tmp_path, meta)
    # no book fed
    await eng._recompute(meta.condition_id)
    assert len(eng.state.orders) == 0
    eng.state.close()
    eng.catalog.close()


# ── fill-model quote filter (pure) ────────────────────────────────────────


def _stub_est():
    from types import SimpleNamespace
    return SimpleNamespace(
        vol=SimpleNamespace(ratio=1.0),
        flow=SimpleNamespace(z=0.0),
        markout=SimpleNamespace(toxicity=0.0),
    )


def _stub_model(deployable: bool, trained: bool = True, should_quote: bool = False):
    from polymaker.strategy.fill_model import FillPrediction

    class _Stub:
        is_deployable = deployable
        is_trained = trained

        def predict(self, feats):
            return FillPrediction(prob_fill=0.95, expected_markout=-0.01,
                                  should_quote=should_quote, suggested_size_mult=2.0)

    return _Stub()


def _filter(meta, quotes, model, *, held=None, now=100.0, risk_cap=800.0):
    from polymaker.engine import _filter_quotes_by_fill_model
    from polymaker.marketdata.orderbook import OrderBook
    from polymaker.strategy.fill_model import FillTrainingStore
    from polymaker.strategy.regime import Regime

    yb = OrderBook(meta.tick_size)
    yb.apply_snapshot(bids=[(0.48, 500), (0.49, 500)],
                      asks=[(0.50, 500), (0.51, 500)], ts=1.0)
    nb = OrderBook(meta.tick_size)
    nb.apply_snapshot(bids=[(0.48, 500), (0.49, 500)],
                      asks=[(0.50, 500), (0.51, 500)], ts=1.0)
    store = FillTrainingStore()
    out = _filter_quotes_by_fill_model(
        quotes, cid=meta.condition_id, meta=meta, tick=meta.tick_size,
        mid=0.495, yes_view=yb.view(), yes_book=yb, no_book=nb,
        yes_token=meta.yes.token_id, est=_stub_est(), fv=0.5,
        now=now, hours_to_end=100.0, regime=Regime.QUIET,
        model=model, store=store, sample_ts={},
        risk_cap_usdc=risk_cap, held=held or {},
    )
    return out, store


def test_fill_model_filter_active_drops_entries_keeps_exits(meta):
    """Deployable model: non-exit quotes are dropped; SELL exits kept + sized."""
    from polymaker.domain import Quote
    yt = meta.yes.token_id
    quotes = [
        Quote(yt, Side.BUY, 0.49, 10.0),     # entry BUY at touch
        Quote(yt, Side.SELL, 0.50, 10.0),    # exit SELL (we hold)
    ]
    out, store = _filter(meta, quotes, _stub_model(deployable=True),
                         held={yt: 10.0})
    assert len(out) == 1
    assert out[0].side is Side.SELL  # exit never removed by the model
    assert out[0].size == 20.0       # but still sized by suggested_size_mult
    # kept quote recorded as a non-fill (online) training sample
    assert len(store.features) == 1
    assert list(store.source) == ["online"]


def test_fill_model_filter_active_sizing_clamped(meta):
    """Sizing is clamped by the per-market notional cap and min-order floor."""
    from polymaker.domain import Quote
    yt = meta.yes.token_id
    quotes = [Quote(yt, Side.BUY, 0.49, 10.0)]
    # cap $4 / 0.49 = 8.16 shares -> min(20, 8.16) = 8.16
    out, _ = _filter(meta, quotes, _stub_model(deployable=True, should_quote=True),
                     held={}, risk_cap=4.0)
    assert out and abs(out[0].size - 8.16) < 0.01
    # cap $1 / 0.49 = 2.04 shares < min_order_size=5 -> floored to 5.0
    out2, _ = _filter(meta, quotes, _stub_model(deployable=True, should_quote=True),
                      held={}, risk_cap=1.0)
    assert out2 and out2[0].size == meta.min_order_size


def test_fill_model_filter_shadow_uses_tree_gate(meta):
    """Trained-but-not-validated model: tree gate still governs, no removal."""
    from polymaker.domain import Quote
    yt = meta.yes.token_id
    quotes = [Quote(yt, Side.BUY, 0.49, 10.0)]
    # Balanced book at 0.495 mid: tree returns 0.0 (mid<=0.6, shallow bid) ->
    # the quote is skipped even though the model is trained (shadow).
    out, _ = _filter(meta, quotes, _stub_model(deployable=False, trained=True))
    assert out == []
    # A cold (untrained) model behaves identically.
    out2, _ = _filter(meta, quotes, _stub_model(deployable=False, trained=False))
    assert out2 == []


def test_fill_model_filter_shadow_logs_but_keeps_good_book(meta):
    """Shadow model may not remove quotes the tree allows."""
    from polymaker.domain import Quote
    from polymaker.engine import _filter_quotes_by_fill_model
    from polymaker.marketdata.orderbook import OrderBook
    from polymaker.strategy.fill_model import FillTrainingStore
    from polymaker.strategy.regime import Regime

    yb = OrderBook(meta.tick_size)
    yb.apply_snapshot(bids=[(0.48, 100), (0.49, 100)],   # thin bids
                      asks=[(0.50, 1000), (0.51, 1000)], ts=1.0)  # deep asks
    nb = OrderBook(meta.tick_size)
    nb.apply_snapshot(bids=[(0.48, 100), (0.49, 100)],
                      asks=[(0.50, 1000), (0.51, 1000)], ts=1.0)
    yt = meta.yes.token_id
    quotes = [Quote(yt, Side.BUY, 0.49, 10.0)]
    out = _filter_quotes_by_fill_model(
        quotes, cid=meta.condition_id, meta=meta, tick=meta.tick_size,
        mid=0.495, yes_view=yb.view(), yes_book=yb, no_book=nb,
        yes_token=yt, est=_stub_est(), fv=0.5, now=100.0, hours_to_end=100.0,
        regime=Regime.QUIET, model=_stub_model(deployable=False, trained=True),
        store=FillTrainingStore(), sample_ts={}, risk_cap_usdc=800.0, held={},
    )
    # imbalance -0.82 <= -0.2 -> tree says trade; shadow model may disagree
    # but must NOT remove the quote.
    assert len(out) == 1
