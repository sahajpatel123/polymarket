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
    # Realistic 2-tick spread, ask-heavy book: the cold-start quality tree
    # (from real at-touch fills) blocks balanced books at the touch (<50% WR),
    # so the synthetic book must look like a tradable one.
    yb = eng.md.book(meta.yes.token_id)
    yb.apply_snapshot(bids=[(0.48, 300), (0.49, 300)], asks=[(0.50, 5000), (0.51, 5000)], ts=now)
    nb = eng.md.book(meta.no.token_id)
    nb.apply_snapshot(bids=[(0.48, 300), (0.49, 300)], asks=[(0.50, 5000), (0.51, 5000)], ts=now)


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


def _stub_model(deployable: bool, trained: bool = True, should_quote: bool = False,
                size_mult: float = 2.0, consensus: float = 0.5):
    from polymaker.strategy.fill_model import FillPrediction

    class _Stub:
        is_deployable = deployable
        is_trained = trained

        def predict(self, feats):
            return FillPrediction(prob_fill=0.95, expected_markout=-0.01,
                                  should_quote=should_quote,
                                  suggested_size_mult=size_mult,
                                  consensus=consensus)

    return _Stub()

def _stub_gov():
    """Neutral win-rate governor: no outcomes yet -> learning mode, no effect."""
    from polymaker.strategy.fill_model import WinRateGovernor

    return WinRateGovernor()


def _filter(meta, quotes, model, *, held=None, now=100.0, risk_cap=800.0,
            gov_policy=None):
    from polymaker.engine import _filter_quotes_by_fill_model
    from polymaker.marketdata.orderbook import OrderBook
    from polymaker.strategy.fill_model import FillTrainingStore
    from polymaker.strategy.regime import Regime

    yb = OrderBook(meta.tick_size)
    yb.apply_snapshot(bids=[(0.48, 300), (0.49, 300)],
                      asks=[(0.50, 5000), (0.51, 5000)], ts=1.0)
    nb = OrderBook(meta.tick_size)
    nb.apply_snapshot(bids=[(0.48, 300), (0.49, 300)],
                      asks=[(0.50, 5000), (0.51, 5000)], ts=1.0)
    store = FillTrainingStore()
    out = _filter_quotes_by_fill_model(
        quotes, cid=meta.condition_id, meta=meta, tick=meta.tick_size,
        mid=0.495, yes_view=yb.view(), yes_book=yb, no_book=nb,
        yes_token=meta.yes.token_id, est=_stub_est(), fv=0.5,
        now=now, hours_to_end=100.0, regime=Regime.QUIET,
        model=model, store=store, gov=_gov_policy(gov_policy) if gov_policy else _stub_gov(),
        sample_ts={},
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
    from polymaker.engine import _filter_quotes_by_fill_model
    from polymaker.marketdata.orderbook import OrderBook
    from polymaker.strategy.fill_model import FillTrainingStore
    from polymaker.strategy.regime import Regime

    yt = meta.yes.token_id
    quotes = [Quote(yt, Side.BUY, 0.49, 10.0)]
    # Bid-heavy book (imbalance +0.82): tree returns 0.0 -> the quote is
    # skipped even though the model is trained (shadow).
    yb = OrderBook(meta.tick_size)
    yb.apply_snapshot(bids=[(0.48, 5000), (0.49, 5000)],
                      asks=[(0.50, 300), (0.51, 300)], ts=1.0)
    nb = OrderBook(meta.tick_size)
    nb.apply_snapshot(bids=[(0.48, 5000), (0.49, 5000)],
                      asks=[(0.50, 300), (0.51, 300)], ts=1.0)

    def _run(model):
        return _filter_quotes_by_fill_model(
            quotes, cid=meta.condition_id, meta=meta, tick=meta.tick_size,
            mid=0.49, yes_view=yb.view(), yes_book=yb, no_book=nb,
            yes_token=yt, est=_stub_est(), fv=0.5, now=100.0, hours_to_end=100.0,
            regime=Regime.QUIET, model=model, store=FillTrainingStore(),
            gov=_stub_gov(), sample_ts={}, risk_cap_usdc=800.0, held={},
        )

    # Trained-but-shadow: tree rejects the book shape -> quote skipped.
    assert _run(_stub_model(deployable=False, trained=True)) == []
    # A cold (untrained) model behaves identically.
    assert _run(_stub_model(deployable=False, trained=False)) == []


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
        store=FillTrainingStore(), gov=_stub_gov(), sample_ts={},
        risk_cap_usdc=800.0, held={},
    )
    # imbalance -0.82 <= -0.2 -> tree says trade; shadow model may disagree
    # but must NOT remove the quote.
    assert len(out) == 1


# ── win-rate governor overlay (closed loop) ───────────────────────────────


def _gov_policy(policy):
    from polymaker.strategy.fill_model import WinRateGovernor

    class _Gov:
        def policy(self):
            return policy

    return _Gov()


def test_fill_model_filter_gov_blocks_entries_keeps_exits(meta):
    """Governor in blocked mode removes entries but never exits."""
    from polymaker.domain import Quote
    from polymaker.strategy.fill_model import GovernorPolicy

    yt = meta.yes.token_id
    quotes = [
        Quote(yt, Side.BUY, 0.49, 10.0),     # entry
        Quote(yt, Side.SELL, 0.50, 10.0),    # exit (held)
    ]
    pol = GovernorPolicy(block_entries=True, consensus_floor=0.0,
                         n_evaluated=30, realized_wr=0.40, mode="blocked")
    out, _ = _filter(meta, quotes, _stub_model(deployable=False),
                     held={yt: 10.0}, gov_policy=pol)
    assert len(out) == 1
    assert out[0].side is Side.SELL


def test_fill_model_filter_gov_consensus_floor_drops_low_consensus(meta):
    """Deployable model + governor consensus floor: low-consensus entries drop."""
    from polymaker.domain import Quote
    from polymaker.strategy.fill_model import FillPrediction, GovernorPolicy

    class _Low:
        is_deployable = True
        is_trained = True

        def predict(self, feats):
            return FillPrediction(prob_fill=0.4, expected_markout=0.001,
                                  should_quote=True, consensus=0.2)

    yt = meta.yes.token_id
    quotes = [Quote(yt, Side.BUY, 0.49, 10.0)]
    pol = GovernorPolicy(block_entries=False, consensus_floor=0.4,
                         n_evaluated=30, realized_wr=0.55, mode="tight")
    out, _ = _filter(meta, quotes, _Low(), held={}, gov_policy=pol)
    assert out == []


def test_fill_model_filter_gov_size_scale_entries_only(meta):
    """Governor size scaling multiplies entries; exits keep their size."""
    from polymaker.domain import Quote
    from polymaker.strategy.fill_model import GovernorPolicy

    yt = meta.yes.token_id
    quotes = [
        Quote(yt, Side.BUY, 0.49, 10.0),    # entry
        Quote(yt, Side.SELL, 0.50, 10.0),   # exit
    ]
    pol = GovernorPolicy(block_entries=False, consensus_floor=0.0,
                         entry_size_scale=0.5, n_evaluated=30,
                         realized_wr=0.60, mode="tight")
    out, _ = _filter(meta, quotes,
                     _stub_model(deployable=True, should_quote=True, size_mult=1.0),
                     held={yt: 10.0}, gov_policy=pol)
    by_side = {q.side: q for q in out}
    assert abs(by_side[Side.BUY].size - 5.0) < 0.01     # entry halved
    assert abs(by_side[Side.SELL].size - 10.0) < 0.01   # exit untouched


# ── deferred fill labels (true forward markout) ───────────────────────────


async def test_pending_fill_labels_resolve_with_forward_markout(tmp_path, meta):
    """A fill's label is resolved from the FV 30s later, not a toxicity EWMA.

    The markout label is the fill MODEL's training target. It is deliberately
    NOT the win-rate governor's control signal: markout measures 30s fair-value
    drift, which scores the maker's core trade (buy the bid, sell the ask) as a
    loss whenever fair value did not move. The governor is driven from realized
    round-trip PnL instead — see test_governor_tracks_realized_round_trip_pnl.
    """
    from polymaker.domain import Fill
    from polymaker.strategy.fill_model import FillTrainingStore

    eng = _engine_with_market(tmp_path, meta)
    _feed_book(eng, meta)
    # Set last_fv directly (estimator default path).
    yt = meta.yes.token_id
    eng.est[meta.condition_id].last_fv = 0.50
    eng.est[meta.condition_id].last_fv_ts = 100.0
    eng.win_gov._min_samples = 1  # single fill is enough for this test

    eng._on_fill(Fill(
        order_id="ord1", token_id=yt, side=Side.BUY,
        price=0.50, size=10.0, ts=100.0, trade_id="t1",
    ))
    # 30s horizon not elapsed at t=105 -> still pending.
    eng._resolve_fill_labels(meta.condition_id, 105.0, 0.50)
    assert not eng.fill_store.features

    # At t=131 the markout resolves against fv_yes=0.52 (we bought -> win).
    eng._resolve_fill_labels(meta.condition_id, 131.0, 0.52)
    assert len(eng.fill_store.features) == 1
    assert eng.fill_store.y_markout[0] > 0.0  # positive markout

    # A SELL fill that resolves lower (token price fell) is also a win.
    no_tok = meta.no.token_id
    eng._on_fill(Fill(
        order_id="ord2", token_id=no_tok, side=Side.SELL,
        price=0.50, size=10.0, ts=200.0, trade_id="t2",
    ))
    eng._resolve_fill_labels(meta.condition_id, 231.0, 0.55)  # fv_yes rose -> NO fell
    assert len(eng.fill_store.features) == 2
    assert eng.fill_store.y_markout[1] > 0.0

    # A BUY fill that resolves against us (price fell) is a loss.
    eng._on_fill(Fill(
        order_id="ord3", token_id=yt, side=Side.BUY,
        price=0.50, size=10.0, ts=300.0, trade_id="t3",
    ))
    eng._resolve_fill_labels(meta.condition_id, 331.0, 0.45)  # fv_yes fell
    assert len(eng.fill_store.features) == 3
    assert eng.fill_store.y_markout[2] < 0.0
    # markout labels must NOT move the governor
    assert eng.win_gov.n_evaluated == 0, (
        "governor was fed markout signs; it must only see realized round trips"
    )

    eng.state.close()
    eng.catalog.close()
