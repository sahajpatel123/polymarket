"""Paper mode must treat local state as the only source of truth.

In paper mode the exchange knows nothing about our orders or positions:
``gateway.open_orders()`` returns ``[]`` and ``gateway.positions()`` /
``token_balances()`` describe the REAL wallet, which is empty. Reconciling
simulated state against those was destructive in three compounding ways:

1. Positions were force-zeroed, so inventory value went to 0 while the cash
   spent remained. Equity collapsed to -(cash spent) and the daily-loss kill
   fired on a loss that did not exist: one run reported -$375 equity while
   realized PnL was -$0.59.
2. With positions at 0 the exit path returned early (``pos.size <
   min_order_size``), so held inventory was never offered for sale.
3. Orders were deleted from ``state.orders`` while the fill simulator still
   held them, so the reconciler re-placed duplicates (one run bought $421
   against a $100 bankroll) and the orphaned originals could never be
   cancelled — they kept filling after cancels and after a risk halt.
"""

from __future__ import annotations

import asyncio

import pytest

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import Fill, MarketMeta, OpenOrder, Side, TokenMeta
from polymaker.engine import Engine
from polymaker.strategy.regime import RegimeMachine

TOK = "yes-token"


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xc", question="q", slug="s",
        tokens=(TokenMeta(TOK, "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.001, neg_risk=False, min_order_size=5.0,
        rewards_min_size=10.0, rewards_max_spread=3.0, rewards_daily_rate=50.0,
        maker_fee_bps=0, taker_fee_bps=100, fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z", event_id="e",
    )


def _engine(tmp_path, *, paper: bool = True) -> Engine:
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "s.db"),
                                  journal_dir=str(tmp_path / "j"),
                                  log_dir=str(tmp_path / "l")))
    cfg.engine.journal = False
    eng = Engine(cfg, paper=paper)
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
    return eng


# ── positions survive ────────────────────────────────────────────────────


def test_paper_equity_is_not_destroyed_by_an_empty_wallet(tmp_path) -> None:
    """Equity must reflect cash + inventory, not cash alone."""
    eng = _engine(tmp_path)
    fill = Fill(TOK, Side.BUY, 0.40, 100.0, "f1", ts=1.0, is_maker=True)
    eng.state.apply_fill(fill)
    eng.risk.note_fill(fill)
    eng.risk.update_mark(TOK, 0.40)

    assert eng.risk.net_cash == pytest.approx(-40.0)
    assert eng.risk.inventory_value == pytest.approx(40.0)
    assert eng.risk.equity == pytest.approx(0.0, abs=1e-9)

    async def empty_wallet(tokens):
        return {t: 0.0 for t in tokens}

    eng.gateway.token_balances = empty_wallet  # type: ignore[method-assign]
    asyncio.run(eng._check_position_divergence())

    assert eng.state.position(TOK).size == pytest.approx(100.0)
    assert eng.risk.inventory_value == pytest.approx(40.0), (
        "inventory was wiped -> equity becomes -(cash spent) -> the daily-loss "
        "kill fires on a phantom loss"
    )
    assert eng.risk.equity == pytest.approx(0.0, abs=1e-9)
    eng.state.close()


def test_exit_is_still_possible_after_a_reconcile_cycle(tmp_path) -> None:
    """Zeroed positions silently disabled the exit path."""
    eng = _engine(tmp_path)
    eng.state.apply_fill(Fill(TOK, Side.BUY, 0.40, 100.0, "f1", ts=1.0))

    async def empty_wallet(tokens):
        return {t: 0.0 for t in tokens}

    eng.gateway.token_balances = empty_wallet  # type: ignore[method-assign]
    asyncio.run(eng._check_position_divergence())
    pos = eng.state.position(TOK)
    assert pos.size >= _meta().min_order_size, (
        "position below the exchange minimum -> _maybe_exit returns early -> "
        "inventory can never be sold"
    )
    eng.state.close()


# ── orders survive ───────────────────────────────────────────────────────


def test_paper_orders_are_not_deleted_by_an_empty_rest_snapshot(tmp_path) -> None:
    """state.orders and the fill simulator must stay in agreement."""
    eng = _engine(tmp_path)
    orders = [
        OpenOrder(f"paper-{i}", TOK, Side.BUY, 0.30, 50.0, created_ts=0.0)
        for i in range(3)
    ]
    for o in orders:
        eng.state.upsert_order(o)
        eng._fill_sim.place(o)
    assert len(eng.state.orders) == 3
    assert len(eng._fill_sim.all_orders()) == 3

    # what the live path would do with an empty REST snapshot
    eng.state.replace_open_orders(TOK, [], grace_s=0.0)
    assert len(eng.state.orders) == 0
    assert len(eng._fill_sim.all_orders()) == 3, (
        "simulator kept the orders while state forgot them — this is the "
        "divergence that produced duplicate exposure and uncancellable orders"
    )


def test_orphaned_simulator_orders_would_still_fill(tmp_path) -> None:
    """Shows the concrete harm: a forgotten order keeps filling."""
    eng = _engine(tmp_path)
    o = OpenOrder("paper-1", TOK, Side.BUY, 0.30, 50.0, created_ts=0.0)
    eng.state.upsert_order(o)
    eng._fill_sim.place(o)
    eng.state.replace_open_orders(TOK, [], grace_s=0.0)   # state forgets it
    # engine cancel paths key off state.orders, so this order can never be
    # withdrawn — and it is still fillable:
    fills = eng._fill_sim.match(TOK, Side.SELL, price=0.29, size=50.0, ts=1.0)
    assert fills, "orphaned order should still fill (that is the bug)"
    eng.state.close()


# ── live mode keeps its reconciliation ───────────────────────────────────


def test_live_mode_still_corrects_positions_to_onchain(tmp_path) -> None:
    """The paper exemption must not weaken live-mode safety."""
    eng = _engine(tmp_path, paper=False)
    eng.state.apply_fill(Fill(TOK, Side.BUY, 0.5, 100.0, "phantom", ts=1.0))

    async def chain(tokens):
        return {t: (5.0 if t == TOK else 0.0) for t in tokens}

    eng.gateway.token_balances = chain  # type: ignore[method-assign]
    asyncio.run(eng._check_position_divergence())
    assert eng.state.position(TOK).size == pytest.approx(5.0)
    eng.state.close()
