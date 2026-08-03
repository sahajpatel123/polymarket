"""The win-rate governor must be controlled by money, not fair-value drift.

The governor throttles entry volume when realized win rate falls below target,
and blocks entries outright below a hard floor. It was fed the sign of a
30-second fair-value markout, which systematically mislabels the maker's core
trade: buying the bid and selling the ask earns a tick even when fair value
never moved, yet scores as a loss.

Observed in a live paper session: 10 resolved markout labels reported
``realized_wr=0.0`` while all 6 completed round trips were profitable. On that
signal the governor would have crossed its hard floor and stopped entering —
cutting trade volume on a metric that disagreed with the money.
"""

from __future__ import annotations

import asyncio

import pytest

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import Fill, MarketMeta, Side, TokenMeta
from polymaker.engine import Engine
from polymaker.strategy.regime import RegimeMachine

TOK = "yes-token"


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xc", question="q", slug="s",
        tokens=(TokenMeta(TOK, "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01, neg_risk=False, min_order_size=5.0,
        rewards_min_size=10.0, rewards_max_spread=3.0, rewards_daily_rate=50.0,
        maker_fee_bps=0, taker_fee_bps=100, fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z", event_id="e",
    )


def _engine(tmp_path) -> Engine:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "s.db"),
                                  journal_dir=str(tmp_path / "j"),
                                  log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    eng = Engine(cfg, paper=True)
    m = _meta()
    cid = m.condition_id
    eng.metas[cid] = m
    eng.profiles[cid] = StrategyProfile()
    eng.est[cid] = Engine._make_estimators(eng.profiles[cid])
    eng.regime_m[cid] = RegimeMachine()
    eng._dirty[cid] = asyncio.Event()
    eng._locks[cid] = asyncio.Lock()
    for t in (TOK, "no-token"):
        eng._token_cid[t] = cid
    eng._running = True
    eng.win_gov._min_samples = 1
    return eng


def _buy(eng: Engine, px: float, sz: float, ts: float, tid: str) -> None:
    f = Fill(TOK, Side.BUY, px, sz, tid, ts=ts, is_maker=True)
    eng.state.apply_fill(f)
    eng._on_fill(f)


def _sell(eng: Engine, px: float, sz: float, ts: float, tid: str) -> None:
    f = Fill(TOK, Side.SELL, px, sz, tid, ts=ts, is_maker=True)
    eng.risk.update_mark(TOK, px)
    eng._on_fill(f)          # note_fill reads avg_price BEFORE apply_fill
    eng.state.apply_fill(f)


def test_spread_capture_counts_as_a_win(tmp_path) -> None:
    """Bought the bid, sold the ask, fair value flat: that is a win."""
    eng = _engine(tmp_path)
    _buy(eng, 0.19, 100.0, 100.0, "b1")
    assert eng.win_gov.n_evaluated == 0, "a buy alone has no outcome yet"
    _sell(eng, 0.20, 100.0, 200.0, "s1")
    assert eng.win_gov.n_evaluated == 1
    assert eng.win_gov.realized_wr == pytest.approx(1.0), (
        "a +1 tick round trip was scored as a loss"
    )
    eng.state.close()


def test_losing_round_trip_counts_as_a_loss(tmp_path) -> None:
    eng = _engine(tmp_path)
    _buy(eng, 0.20, 100.0, 100.0, "b1")
    _sell(eng, 0.18, 100.0, 200.0, "s1")
    assert eng.win_gov.n_evaluated == 1
    assert eng.win_gov.realized_wr == pytest.approx(0.0)
    eng.state.close()


def test_markout_labels_do_not_touch_the_governor(tmp_path) -> None:
    """Model training labels and the volume control signal are separate.

    (That markout labels still reach the fill store is covered by
    test_engine.py::test_pending_fill_labels_resolve_with_forward_markout,
    which sets up the book the feature extractor needs.)
    """
    eng = _engine(tmp_path)
    cid = _meta().condition_id
    eng.est[cid].last_fv = 0.50
    eng.est[cid].last_fv_ts = 100.0
    _buy(eng, 0.50, 10.0, 100.0, "b1")
    eng._resolve_fill_labels(cid, 131.0, 0.45)   # adverse 30s markout
    assert eng.win_gov.n_evaluated == 0, (
        "markout sign moved the governor — this is what reported 0% win rate "
        "while every round trip was profitable"
    )
    eng.state.close()


def test_naked_short_is_not_scored_as_a_round_trip(tmp_path) -> None:
    """A sell with no cost basis has no outcome; scoring it throttles wrongly."""
    eng = _engine(tmp_path)
    _sell(eng, 0.50, 10.0, 100.0, "s1")
    assert eng.win_gov.n_evaluated == 0
    eng.state.close()


def test_mixed_outcomes_produce_a_real_win_rate(tmp_path) -> None:
    eng = _engine(tmp_path)
    for i, (buy_px, sell_px) in enumerate(
        [(0.19, 0.20), (0.19, 0.20), (0.19, 0.20), (0.20, 0.18)]
    ):
        _buy(eng, buy_px, 50.0, 100.0 + i * 10, f"b{i}")
        _sell(eng, sell_px, 50.0, 105.0 + i * 10, f"s{i}")
    assert eng.win_gov.n_evaluated == 4
    assert eng.win_gov.realized_wr == pytest.approx(0.75)
    eng.state.close()


def test_governor_does_not_block_on_a_profitable_book(tmp_path) -> None:
    """The failure mode: throttling entries while actually making money."""
    eng = _engine(tmp_path)
    eng.win_gov._min_samples = 5
    for i in range(6):
        _buy(eng, 0.19, 50.0, 100.0 + i * 10, f"b{i}")
        _sell(eng, 0.20, 50.0, 105.0 + i * 10, f"s{i}")
    pol = eng.win_gov.policy()
    assert pol.realized_wr == pytest.approx(1.0)
    assert pol.block_entries is False, (
        "governor blocked entries on a 100% profitable book"
    )
    assert pol.mode != "blocked"
    eng.state.close()


# ── full closes must be scored ────────────────────────────────────────────


def test_full_close_is_scored_not_dropped(tmp_path) -> None:
    """apply_fill zeroes avg_price on a full close.

    Reading the cost basis after that point returns 0, which looked like "no
    position" and made the round trip unscoreable — so exactly the trades that
    completed were the ones the governor never learned from. The caller now
    captures the basis before applying the fill.
    """
    eng = _engine(tmp_path)
    _buy(eng, 0.19, 100.0, 100.0, "b1")
    # close the ENTIRE position
    f = Fill(TOK, Side.SELL, 0.20, 100.0, "s1", ts=200.0, is_maker=True)
    pre = eng.state.position(TOK)
    basis = pre.avg_price if pre.size > 0 else 0.0
    eng.state.apply_fill(f)
    assert eng.state.position(TOK).size == pytest.approx(0.0)
    assert eng.state.position(TOK).avg_price == pytest.approx(0.0), (
        "precondition: avg_price is zeroed on a full close"
    )
    eng._on_fill(f, cost_basis=basis)
    assert eng.win_gov.n_evaluated == 1, "full close was not scored"
    assert eng.win_gov.realized_wr == pytest.approx(1.0)
    eng.state.close()


def test_note_fill_without_cost_basis_falls_back_to_position(tmp_path) -> None:
    """Partial closes still work when the caller passes nothing."""
    eng = _engine(tmp_path)
    _buy(eng, 0.19, 100.0, 100.0, "b1")
    f = Fill(TOK, Side.SELL, 0.20, 40.0, "s1", ts=200.0, is_maker=True)
    realized = eng.risk.note_fill(f)          # no cost_basis kwarg
    assert realized is not None
    assert realized == pytest.approx((0.20 - 0.19) * 40.0)
    eng.state.close()


def test_default_tracker_callback_tolerates_cost_basis(tmp_path) -> None:
    """The no-op default must not raise when the processor passes kwargs."""
    from polymaker.state.store import StateStore
    from polymaker.state.tracker import UserEventProcessor

    store = StateStore(tmp_path / "t.db")
    proc = UserEventProcessor(store)           # no on_fill supplied
    proc._on_fill(Fill(TOK, Side.BUY, 0.5, 10.0, "x"), cost_basis=0.5)
    store.close()
