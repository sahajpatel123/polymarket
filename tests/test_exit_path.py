"""Tests for the exit path: reachability, P&L awareness, and hold-time survival.

Every fill in the observed live sessions was a BUY — 168 buys, zero sells. Three
independent defects caused that, and each is pinned here:

1. ``_maybe_exit`` capped nothing, so with a realistic half-spread the ask rested
   9-49 ticks above the book and could never be hit.
2. The exit target never referenced ``pos.avg_price``, so it had no notion of
   being in profit or in loss.
3. Take-profit/stop-loss registration crashed with AttributeError on every BUY
   fill (``Quote.model_copy`` on a frozen dataclass), so the profit/loss exit
   never existed at all.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math

import pytest

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import (
    Fill,
    MarketMeta,
    Position,
    Quote,
    Regime,
    Side,
    TokenMeta,
)
from polymaker.engine import Engine
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.quoting import (
    _maybe_exit,
    clamp_sell_exposure,
    compute_tp_sl,
)
from polymaker.strategy.regime import RegimeMachine

TICK = 0.001
DEC = 3
TOK = "yes-token"


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xc", question="q", slug="s",
        tokens=(TokenMeta(TOK, "Yes"), TokenMeta("no-token", "No")),
        tick_size=TICK, neg_risk=False, min_order_size=5.0,
        rewards_min_size=10.0, rewards_max_spread=3.0, rewards_daily_rate=50.0,
        maker_fee_bps=0, taker_fee_bps=100, fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z", event_id="e",
    )


def _view(bid: float = 0.311, ask: float = 0.313) -> BookView:
    return BookView(best_bid=bid, best_bid_size=500, best_ask=ask,
                    best_ask_size=500, second_bid=bid - TICK,
                    second_ask=ask + TICK, bid_depth=5000, ask_depth=5000)


def _exit(cost: float, fv: float, urgency: float, *, delta: float = 0.030,
          slp: float = 0.015, regime: Regime = Regime.QUIET,
          size: float = 113.2) -> Quote | None:
    quotes: list[Quote] = []
    _maybe_exit(quotes, TOK, Position(TOK, size, cost), fv, delta, _view(),
                TICK, DEC, urgency, _meta(), regime, stop_loss_pct=slp)
    return quotes[0] if quotes else None


# ── 1. reachability ──────────────────────────────────────────────────────


@pytest.mark.parametrize("delta", [0.002, 0.010, 0.030, 0.050, 0.100])
def test_exit_is_never_parked_above_the_ask(delta: float) -> None:
    """The exit must be hittable regardless of half-spread.

    Before: delta=0.050 put the ask 49 ticks above the book, so inventory could
    never be sold at any urgency below 1.0.
    """
    q = _exit(cost=0.300, fv=0.312, urgency=0.0, delta=delta)
    assert q is not None
    assert q.price <= _view().best_ask, (
        f"exit at {q.price} is above the ask {_view().best_ask} with "
        f"delta={delta} — unreachable, so the position is never exited"
    )


def test_profitable_exit_rests_at_the_touch_when_fresh() -> None:
    q = _exit(cost=0.300, fv=0.312, urgency=0.0)
    assert q is not None
    assert q.price == pytest.approx(0.313)      # == best_ask
    assert q.price > 0.300, "must still be above cost"


# ── 2. P&L awareness ─────────────────────────────────────────────────────


def test_does_not_offer_below_cost_while_time_remains() -> None:
    """A small loss inside the stop must be held, not crystallised."""
    cost, fv = 0.315, 0.313          # -0.6%, stop is 1.5%
    for urgency in (0.0, 0.25, 0.5, 0.9):
        q = _exit(cost=cost, fv=fv, urgency=urgency)
        assert q is not None
        assert q.price > cost, (
            f"urgency={urgency}: exit at {q.price} is at/below cost {cost} — "
            "realises a loss with time still on the clock"
        )


def test_time_stop_accepts_a_loss_once_urgency_is_exhausted() -> None:
    """At urgency 1.0 the position must actually leave, loss or not."""
    q = _exit(cost=0.315, fv=0.313, urgency=1.0)
    assert q is not None
    assert q.price <= _view().best_ask, "time stop must produce a fillable exit"
    assert q.price == pytest.approx(_view().best_bid + TICK)


def test_stop_breach_exits_immediately_regardless_of_urgency() -> None:
    """Past the stop, patience is wrong — leave at the most aggressive maker px."""
    q = _exit(cost=0.330, fv=0.312, urgency=0.0, slp=0.015)   # -5.5% vs 1.5%
    assert q is not None
    assert q.price == pytest.approx(_view().best_bid + TICK)
    assert q.price <= _view().best_ask


def test_stop_disabled_means_no_forced_exit() -> None:
    """slp=0 must not be read as 'always stopped'."""
    q = _exit(cost=0.330, fv=0.312, urgency=0.0, slp=0.0)
    assert q is not None
    assert q.price > 0.330, "with the stop disabled, hold above cost"


def test_reduce_only_regime_produces_a_fillable_exit() -> None:
    q = _exit(cost=0.320, fv=0.312, urgency=0.0, regime=Regime.REDUCE_ONLY)
    assert q is not None
    assert q.price <= _view().best_ask


def test_exit_never_sells_more_than_held() -> None:
    q = _exit(cost=0.300, fv=0.312, urgency=1.0, size=113.267)
    assert q is not None
    assert q.size <= 113.267
    assert q.size == math.floor(113.267 * 100) / 100


def test_no_exit_below_exchange_minimum() -> None:
    assert _exit(cost=0.300, fv=0.312, urgency=1.0, size=1.0) is None


# ── 3. take-profit / stop-loss registration ──────────────────────────────


def _engine(tmp_path, tp: float = 0.02, sl: float = 0.015) -> Engine:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "s.db"),
                                  journal_dir=str(tmp_path / "j"),
                                  log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    eng = Engine(cfg, paper=True)
    m = _meta()
    cid = m.condition_id
    eng.metas[cid] = m
    eng.profiles[cid] = StrategyProfile(take_profit_pct=tp, stop_loss_pct=sl,
                                        max_risk_per_trade_usdc=3.0)
    eng.est[cid] = Engine._make_estimators(eng.profiles[cid])
    eng.regime_m[cid] = RegimeMachine()
    eng._dirty[cid] = asyncio.Event()
    eng._locks[cid] = asyncio.Lock()
    for t in (TOK, "no-token"):
        eng._token_cid[t] = cid
    eng._running = True
    return eng


def test_buy_fill_registers_tp_and_sl(tmp_path) -> None:
    """Regression: this raised AttributeError on every single BUY fill."""
    eng = _engine(tmp_path)
    f = Fill(TOK, Side.BUY, 0.312, 100.0, "t1", ts=1000.0, is_maker=True,
             order_id="paper-1")
    eng.state.apply_fill(f)
    eng._on_fill(f)          # must not raise
    tp, sl = eng._tp_sl_targets[eng.metas["0xc"].condition_id][TOK]
    assert tp is not None and sl is not None
    assert tp.token_id == TOK and sl.token_id == TOK
    assert tp.price > 0.312, "take-profit must be above entry"
    assert sl.price < 0.312, "stop-loss must be below entry"
    assert tp.side is Side.SELL and sl.side is Side.SELL
    eng.state.close()


def test_on_fill_completes_so_metrics_are_emitted(tmp_path) -> None:
    """The crash aborted _on_fill, so the 'fill' metric was never written."""
    eng = _engine(tmp_path)
    seen: list[str] = []
    eng.metrics.emit = lambda name, **kw: seen.append(name)  # type: ignore[method-assign]
    f = Fill(TOK, Side.BUY, 0.312, 100.0, "t2", ts=1000.0, is_maker=True,
             order_id="paper-2")
    eng.state.apply_fill(f)
    eng._on_fill(f)
    assert "fill" in seen, "fill metric lost — _on_fill aborted before emitting"
    eng.state.close()


def test_sell_fill_clears_tp_sl(tmp_path) -> None:
    eng = _engine(tmp_path)
    cid = "0xc"
    buy = Fill(TOK, Side.BUY, 0.312, 100.0, "b1", ts=1000.0, is_maker=True)
    eng.state.apply_fill(buy)
    eng._on_fill(buy)
    assert TOK in eng._tp_sl_targets[cid]
    sell = Fill(TOK, Side.SELL, 0.320, 100.0, "s1", ts=1100.0, is_maker=True)
    eng.state.apply_fill(sell)
    eng._on_fill(sell)
    assert TOK not in eng._tp_sl_targets.get(cid, {})
    eng.state.close()


def test_compute_tp_sl_caps_risk_by_size(tmp_path) -> None:
    tp, sl = compute_tp_sl(fill_price=0.50, fill_size=1000.0, fv=0.50,
                           tp_pct=0.02, sl_pct=0.02, max_risk_usdc=5.0,
                           tick=TICK, dec=DEC)
    assert sl is not None
    risk = (0.50 - sl.price) * sl.size
    assert risk <= 5.0 + 1e-6, f"stop risks {risk} > cap 5.0"


# ── 4. hold time survives a restart ──────────────────────────────────────


def test_position_entry_ts_reconstructed_from_fills(tmp_path) -> None:
    """Exit urgency is hold-time driven; it must survive a process restart."""
    eng = _engine(tmp_path)
    for i, ts in enumerate((1000.0, 1200.0, 1400.0)):
        f = Fill(TOK, Side.BUY, 0.30, 50.0, f"b{i}", ts=ts, is_maker=True)
        eng.state.apply_fill(f)
    assert eng.state.position_entry_ts(TOK) == 1000.0, (
        "entry time must be the fill that opened the position, not the latest"
    )
    eng.state.close()


def test_position_entry_ts_resets_after_going_flat(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.state.apply_fill(Fill(TOK, Side.BUY, 0.30, 50.0, "b1", ts=1000.0))
    eng.state.apply_fill(Fill(TOK, Side.SELL, 0.31, 50.0, "s1", ts=1100.0))
    assert eng.state.position_entry_ts(TOK) is None, "flat position has no age"
    eng.state.apply_fill(Fill(TOK, Side.BUY, 0.30, 20.0, "b2", ts=2000.0))
    assert eng.state.position_entry_ts(TOK) == 2000.0, (
        "a re-opened position must be aged from the NEW entry"
    )
    eng.state.close()


def test_restore_position_ages_populates_engine_state(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng.state.apply_fill(Fill(TOK, Side.BUY, 0.30, 50.0, "b1", ts=1000.0))
    eng._pos_entry_ts.clear()          # simulate a fresh process
    eng._restore_position_ages()
    assert eng._pos_entry_ts.get(TOK) == 1000.0, (
        "restart left position age unset -> urgency stays 0 -> exit never "
        "reaches its time stop"
    )
    eng.state.close()


def test_urgency_reaches_one_for_a_long_held_position(tmp_path) -> None:
    """End to end: an old position must produce a fillable exit."""
    eng = _engine(tmp_path)
    eng.state.apply_fill(Fill(TOK, Side.BUY, 0.30, 50.0, "b1", ts=1000.0))
    eng._restore_position_ages()
    now = 1000.0 + 3600.0                      # held an hour
    hold = now - eng._pos_entry_ts[TOK]
    urgency = min(1.0, hold / StrategyProfile().exit_urgency_s)
    assert urgency == 1.0
    q = _exit(cost=0.30, fv=0.312, urgency=urgency)
    assert q is not None and q.price <= _view().best_ask
    eng.state.close()


def test_quote_is_a_frozen_dataclass_not_pydantic() -> None:
    """Pins the assumption the crash violated."""
    q = Quote(TOK, Side.SELL, 0.5, 10.0)
    assert not hasattr(q, "model_copy")
    assert dataclasses.replace(q, token_id="other").token_id == "other"


# ── 5. total sell exposure never exceeds inventory ───────────────────────


def test_sell_exposure_clamped_to_holding() -> None:
    """Observed live: 73.84 shares offered against a 27.57 holding.

    The unwind, the take-profit and the stop-loss are each capped against the
    position individually, so together they oversell. There is no OCO, so in
    paper every leg fills and the long flips short.
    """
    quotes = [
        Quote(TOK, Side.SELL, 0.20, 29.00),
        Quote(TOK, Side.SELL, 0.20, 20.84),
        Quote(TOK, Side.SELL, 0.18, 24.00),
        Quote(TOK, Side.BUY, 0.17, 50.00),
    ]
    out = clamp_sell_exposure(quotes, {TOK: 27.57}, min_order_size=5.0)
    sold = sum(q.size for q in out if q.side is Side.SELL)
    assert sold <= 27.57 + 1e-9, f"offered {sold} against 27.57 held"
    assert any(q.side is Side.BUY and q.size == 50.0 for q in out), (
        "entry quotes must pass through untouched"
    )


def test_clamp_prioritises_the_most_aggressive_exit() -> None:
    """Scarce inventory should back risk reduction, not the optimistic TP."""
    quotes = [
        Quote(TOK, Side.SELL, 0.25, 100.0),   # take-profit
        Quote(TOK, Side.SELL, 0.18, 100.0),   # stop / unwind
    ]
    out = clamp_sell_exposure(quotes, {TOK: 50.0}, min_order_size=5.0)
    sells = [q for q in out if q.side is Side.SELL]
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(0.18)
    assert sells[0].size == pytest.approx(50.0)


def test_clamp_trims_rather_than_drops_when_partial_room_exists() -> None:
    quotes = [Quote(TOK, Side.SELL, 0.18, 100.0)]
    out = clamp_sell_exposure(quotes, {TOK: 30.0}, min_order_size=5.0)
    assert len(out) == 1
    assert out[0].size == pytest.approx(30.0)


def test_clamp_drops_legs_below_exchange_minimum() -> None:
    quotes = [
        Quote(TOK, Side.SELL, 0.18, 28.0),
        Quote(TOK, Side.SELL, 0.20, 28.0),
    ]
    out = clamp_sell_exposure(quotes, {TOK: 30.0}, min_order_size=5.0)
    sells = [q for q in out if q.side is Side.SELL]
    assert len(sells) == 1, "2-share remainder is unsellable, must be dropped"
    assert sells[0].size == pytest.approx(28.0)


def test_clamp_leaves_shorts_alone() -> None:
    """A SELL in a token we do not hold is not an exit; do not touch it."""
    quotes = [Quote("other-token", Side.SELL, 0.5, 10.0)]
    out = clamp_sell_exposure(quotes, {TOK: 100.0}, min_order_size=5.0)
    assert out == quotes


def test_clamp_is_a_noop_without_sells() -> None:
    quotes = [Quote(TOK, Side.BUY, 0.1, 10.0), Quote(TOK, Side.BUY, 0.2, 20.0)]
    assert clamp_sell_exposure(quotes, {TOK: 0.0}, min_order_size=5.0) == quotes


# ── 6. coarse-tick markets ───────────────────────────────────────────────


def _coarse_meta() -> MarketMeta:
    return dataclasses.replace(_meta(), tick_size=0.01)


def _coarse_exit(cost: float, fv: float, urgency: float, *, slp: float = 0.015,
                 size: float = 202.0) -> Quote | None:
    """tick=0.01 on a ~$0.19 asset — one tick is 5.3% of price."""
    tick = 0.01
    bid, ask = round(fv - 0.005, 2), round(fv + 0.005, 2)
    view = BookView(best_bid=bid, best_bid_size=500, best_ask=ask,
                    best_ask_size=500, second_bid=bid - tick,
                    second_ask=ask + tick, bid_depth=5000, ask_depth=5000)
    quotes: list[Quote] = []
    _maybe_exit(quotes, TOK, Position(TOK, size, cost), fv, 0.01, view, tick, 2,
                urgency, _coarse_meta(), Regime.QUIET, stop_loss_pct=slp)
    return quotes[0] if quotes else None


def test_coarse_tick_stop_is_not_tripped_by_one_tick_of_noise() -> None:
    """A 1.5% stop is finer than a $0.01 tick, so it fired on every downtick.

    Observed: bought 202 @ 0.19 and the position was dumped at 0.18 for -$2.02
    within 104 seconds. The stop distance is now floored at 2 ticks.
    """
    q = _coarse_exit(cost=0.19, fv=0.18, urgency=0.0)
    assert q is not None
    assert q.price > 0.19, (
        f"exit at {q.price} realises a loss on a single tick of noise"
    )


def test_coarse_tick_stop_still_fires_on_a_real_move() -> None:
    q = _coarse_exit(cost=0.19, fv=0.17, urgency=0.0)   # -2 ticks
    assert q is not None
    assert q.price <= 0.18, "a genuine 2-tick adverse move must be cut"


def test_coarse_tick_flat_market_offers_a_one_tick_maker_profit() -> None:
    """Buying the bid and offering the ask is the maker's edge."""
    q = _coarse_exit(cost=0.19, fv=0.19, urgency=0.0)
    assert q is not None
    assert q.price == pytest.approx(0.20)
    assert (q.price - 0.19) * q.size > 0
