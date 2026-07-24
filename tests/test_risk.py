"""Tests for the RiskManager gates and circuit breakers."""

from __future__ import annotations

from polymaker.config import RiskConfig
from polymaker.domain import Fill, Side
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore


def _rm(tmp_path, **over):
    cfg = RiskConfig(**{
        "max_total_exposure_usdc": 5000, "max_market_notional_usdc": 800,
        "max_event_group_loss_usdc": 1000, "daily_loss_kill_usdc": 250,
        **over,
    })
    store = StateStore(tmp_path / "s.db")
    return RiskManager(cfg, store), store


def test_daily_loss_kill_switch(tmp_path, meta):
    rm, store = _rm(tmp_path)
    # buy 1000 shares @ 0.50 -> -500 cash, +1000 inventory
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 1000, "t1"))
    rm.note_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 1000, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.reset_day()
    assert rm.global_halt()[0] is False
    # fair value collapses to 0.20 -> unrealized loss 300 > 250 kill
    rm.update_mark(meta.yes.token_id, 0.20)
    halted, why = rm.global_halt()
    assert halted and "daily_loss" in why
    store.close()


def test_market_cap_triggers_reduce_only(tmp_path, meta):
    rm, store = _rm(tmp_path, max_market_notional_usdc=100)
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 300, "t1"))  # 150 notional > 100
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.update_mark(meta.no.token_id, 0.50)
    d = rm.evaluate(meta, ws_stale=False, event_group_cost=0.0)
    assert d.reduce_only and d.reason == "market_cap"
    store.close()


def test_ws_stale_halts_market(tmp_path, meta):
    rm, store = _rm(tmp_path)
    d = rm.evaluate(meta, ws_stale=True, event_group_cost=0.0)
    assert d.halt and d.reason == "ws_stale"
    store.close()


def test_size_scale_tapers_near_cap(tmp_path, meta):
    rm, store = _rm(tmp_path, max_market_notional_usdc=100)
    # 85 notional -> 85% of cap -> should scale below 1.0 but not reduce-only
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 170, "t1"))  # 85 notional
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.update_mark(meta.no.token_id, 0.50)
    d = rm.evaluate(meta, ws_stale=False, event_group_cost=0.0)
    assert not d.reduce_only
    assert 0.0 < d.size_scale < 1.0
    store.close()


def test_event_group_cap(tmp_path, meta):
    rm, store = _rm(tmp_path, max_event_group_loss_usdc=50)
    d = rm.evaluate(meta, ws_stale=False, event_group_cost=60.0)
    assert d.reduce_only and d.reason == "event_group_cap"
    store.close()


def test_error_rate_breaker(tmp_path, meta):
    rm, store = _rm(tmp_path, max_order_error_rate=0.25)
    for _ in range(15):
        rm.note_order_result(False)
    for _ in range(10):
        rm.note_order_result(True)  # 15/25 = 0.6 > 0.25
    assert rm.global_halt()[0] is True
    store.close()


def test_manual_kill(tmp_path, meta):
    rm, store = _rm(tmp_path)
    rm.kill()
    assert rm.global_halt() == (True, "manual_kill")
    store.close()


def test_concentration_limit_triggers_reduce_only(tmp_path, meta):
    """Single market with >50% of total exposure should trigger reduce-only.

    Without this guard, a $30 account could allocate 100% to one toxic
    market and lose everything. The fix: max_market_concentration_pct=0.5.
    """
    rm, store = _rm(
        tmp_path,
        max_market_notional_usdc=1000,  # high hard cap so concentration triggers first
        max_total_exposure_usdc=200,
        max_market_concentration_pct=0.5,
    )
    # 150 notional in one market = 75% of total (200) > 50% concentration cap
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 300, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.update_mark(meta.no.token_id, 0.50)
    d = rm.evaluate(meta, ws_stale=False, event_group_cost=0.0)
    assert d.reduce_only
    assert "concentration" in d.reason
    store.close()


def test_per_market_loss_kill_switch(tmp_path, meta):
    """Per-market unrealized loss > max_market_loss triggers reduce-only."""
    rm, store = _rm(tmp_path, max_market_loss_usdc=2.0)
    # Buy 100 shares at 0.50
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 100, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    # Price drops to 0.45 -> unrealized loss = 0.05 * 100 = $5
    rm.update_mark(meta.yes.token_id, 0.45)
    d = rm.evaluate(meta, ws_stale=False, event_group_cost=0.0)
    assert d.reduce_only
    assert "market_loss" in d.reason
    store.close()


def test_gas_cost_circuit_breaker(tmp_path, meta):
    """Cumulative gas cost > max_gas_cost_pct of starting equity triggers global halt.

    On Polygon, a single merge tx can cost $1-5. With $30 capital, one
    bad merge = 3-17% gone. This circuit breaker prevents that.
    """
    rm, store = _rm(tmp_path, max_gas_cost_pct=0.10, max_total_exposure_usdc=100)
    # Buy some inventory to establish equity ($500 from 1000 shares at 0.50)
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 1000, "t1"))
    rm.note_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 1000, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.reset_day()  # equity is now $500 (net cash -500 + inventory 500 = 0)
    # Use a smaller cap to make the test deterministic
    # The fallback uses max_total_exposure_usdc=100 as reference when
    # day_start_equity is 0. So 10% of $100 = $10.
    rm.note_gas_cost(5)    # 5% of $100 — below threshold
    rm.note_gas_cost(7)    # cumulative 12% — above threshold
    halted, reason = rm.global_halt()
    assert halted
    assert "gas_cost" in reason
    store.close()


def test_gas_cost_below_threshold_no_halt(tmp_path, meta):
    """Gas costs below threshold should not trigger global halt."""
    rm, store = _rm(tmp_path, max_gas_cost_pct=0.10, max_total_exposure_usdc=100)
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 1000, "t1"))
    rm.note_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 1000, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.reset_day()
    rm.note_gas_cost(3)  # 3% of $100 — well below 10% threshold
    assert not rm.global_halt()[0]
    store.close()


def test_note_gas_cost_accumulates(tmp_path, meta):
    """note_gas_cost should accumulate cumulatively."""
    rm, store = _rm(tmp_path)
    rm.note_gas_cost(1.5)
    rm.note_gas_cost(2.0)
    rm.note_gas_cost(0.5)
    assert abs(rm.cumulative_gas_cost - 4.0) < 0.001
    store.close()


def test_bankroll_scales_absolute_caps():
    """bankroll_usdc > 0 derives absolute USDC caps from fractions."""
    cfg = RiskConfig(
        bankroll_usdc=1000.0,
        total_exposure_frac=1.0,
        market_notional_frac=0.35,
        event_group_frac=0.5,
        daily_loss_frac=0.1,
        market_loss_frac=0.05,
        max_market_concentration_pct=0.5,
    ).resolve_from_bankroll()
    assert abs(cfg.max_total_exposure_usdc - 1000.0) < 1e-6
    # market notional = min(0.35*1000, 1.0*1000*0.5) = min(350, 500) = 350
    assert abs(cfg.max_market_notional_usdc - 350.0) < 1e-6
    assert abs(cfg.max_event_group_loss_usdc - 500.0) < 1e-6
    assert abs(cfg.daily_loss_kill_usdc - 100.0) < 1e-6
    assert abs(cfg.max_market_loss_usdc - 50.0) < 1e-6


def test_bankroll_zero_keeps_absolute_caps():
    cfg = RiskConfig(
        bankroll_usdc=0.0,
        max_total_exposure_usdc=777.0,
        max_market_notional_usdc=111.0,
    ).resolve_from_bankroll()
    assert cfg.max_total_exposure_usdc == 777.0
    assert cfg.max_market_notional_usdc == 111.0


def test_scale_profile_sizes_follows_bankroll():
    from polymaker.config import StrategyProfile

    cfg = RiskConfig(bankroll_usdc=500.0, market_notional_frac=0.35).resolve_from_bankroll()
    p = StrategyProfile(base_size_usdc=3.0, q_max_usdc=30.0, bankroll_usdc=0.0)
    scaled = cfg.scale_profile_sizes(p)
    assert scaled.bankroll_usdc == 500.0
    assert scaled.base_size_usdc == max(2.0, min(250.0, 500.0 * 0.10))
    assert scaled.q_max_usdc == cfg.max_market_notional_usdc
    assert scaled.bankroll_usdc == 500.0


def test_bankroll_scales_at_100_and_1000():
    """Acceptance: two bankrolls produce proportional absolute caps."""
    small = RiskConfig(
        bankroll_usdc=100.0,
        market_notional_frac=0.4,
        daily_loss_frac=0.1,
        max_market_concentration_pct=1.0,
    ).resolve_from_bankroll()
    large = RiskConfig(
        bankroll_usdc=1000.0,
        market_notional_frac=0.4,
        daily_loss_frac=0.1,
        max_market_concentration_pct=1.0,
    ).resolve_from_bankroll()
    assert abs(small.max_market_notional_usdc - 40.0) < 1e-6
    assert abs(large.max_market_notional_usdc - 400.0) < 1e-6
    assert abs(large.daily_loss_kill_usdc / small.daily_loss_kill_usdc - 10.0) < 1e-6
    from polymaker.config import StrategyProfile
    p100 = small.scale_profile_sizes(StrategyProfile())
    p1000 = large.scale_profile_sizes(StrategyProfile())
    assert p1000.base_size_usdc > p100.base_size_usdc
    assert p1000.q_max_usdc > p100.q_max_usdc


def test_risk_manager_uses_resolved_bankroll(tmp_path, meta):
    """RiskManager constructor resolves bankroll so evaluate uses scaled caps."""
    rm, store = _rm(
        tmp_path,
        bankroll_usdc=200.0,
        market_notional_frac=0.25,
        max_market_concentration_pct=1.0,  # concentration = market_notional_frac only
        daily_loss_frac=0.1,
        market_loss_frac=0.05,
        total_exposure_frac=1.0,
        event_group_frac=0.5,
    )
    # max market notional = 200 * 0.25 = 50
    assert abs(rm.cfg.max_market_notional_usdc - 50.0) < 1e-6
    # 60 notional > 50 -> market_cap reduce-only
    store.apply_fill(Fill(meta.yes.token_id, Side.BUY, 0.50, 120, "t1"))
    rm.update_mark(meta.yes.token_id, 0.50)
    rm.update_mark(meta.no.token_id, 0.50)
    d = rm.evaluate(meta, ws_stale=False, event_group_cost=0.0)
    assert d.reduce_only
    assert d.reason in ("market_cap",) or "concentration" in d.reason or "market" in d.reason
    store.close()
