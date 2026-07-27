"""Tests for V3 percent-based risk policy.

The policy is the single source of truth for risk. Every other module
sizing/orchestrating capital reads from it. These tests verify the
math is correct, the env overrides work, and the resolved values
match the percentages they came from.

Importantly: there is no profit cap and no target growth field. The
bot earns without ceiling. Tests here cover only the loss-side caps.
"""

from __future__ import annotations

import os

import pytest

from polymaker.intelligence.policy import (
    DEFAULT_DAILY_LOSS_KILL_PCT,
    DEFAULT_MAX_PER_MARKET_PCT,
    VALID_RISK_PROFILES,
    RiskPolicy,
    RiskProfile,
    load_capital_usdc,
)

# ── RiskProfile ──────────────────────────────────────────────────────


def test_risk_profile_balanced_defaults():
    p = RiskProfile.from_name("balanced")
    assert p.size_mult == 1.0
    assert p.loss_kill_mult == 1.0
    assert p.max_markets == 8


def test_risk_profile_conservative_tighter():
    p = RiskProfile.from_name("conservative")
    assert p.size_mult == 0.5
    assert p.loss_kill_mult == 0.5
    assert p.max_markets < RiskProfile.from_name("balanced").max_markets


def test_risk_profile_aggressive_looser():
    p = RiskProfile.from_name("aggressive")
    assert p.size_mult == 2.0
    assert p.loss_kill_mult == 2.0
    assert p.max_markets > RiskProfile.from_name("balanced").max_markets


def test_risk_profile_unknown_raises():
    with pytest.raises(ValueError):
        RiskProfile.from_name("yolo")
    with pytest.raises(ValueError):
        RiskProfile.from_name("")


def test_all_valid_profiles_construct():
    for name in VALID_RISK_PROFILES:
        p = RiskProfile.from_name(name)
        assert p.name == name
        assert p.size_mult > 0
        assert p.loss_kill_mult > 0


# ── RiskPolicy has NO target/profit-cap field ───────────────────────


def test_policy_has_no_target_field():
    """The bot earns without ceiling. No target growth, ever."""
    p = RiskPolicy()
    fields = {f.name for f in p.__dataclass_fields__.values()}
    assert "target_daily_growth_pct" not in fields
    assert "target_pct" not in fields
    assert "profit_cap_pct" not in fields
    assert "max_profit_usdc" not in fields


def test_resolved_has_no_target_field():
    """The resolved view also has no profit cap."""
    r = RiskPolicy().resolve(100.0)
    fields = {f.name for f in r.__dataclass_fields__.values()}
    assert "target_daily_growth_usdc" not in fields
    assert "max_profit_usdc" not in fields


# ── RiskPolicy defaults and env overrides ────────────────────────────


def test_policy_default_factory_values():
    p = RiskPolicy()
    assert p.max_per_market_pct == DEFAULT_MAX_PER_MARKET_PCT
    assert p.daily_loss_kill_pct == DEFAULT_DAILY_LOSS_KILL_PCT
    assert p.max_concurrent_markets == 8
    assert p.profile_name == "balanced"


def test_policy_env_override_pct_form():
    os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"] = "0.08"
    try:
        p = RiskPolicy.from_env()
        assert abs(p.daily_loss_kill_pct - 0.08) < 1e-9
    finally:
        del os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"]


def test_policy_env_override_whole_percent():
    """User can pass '8' meaning 8%, not 800%."""
    os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"] = "8"
    try:
        p = RiskPolicy.from_env()
        assert abs(p.daily_loss_kill_pct - 0.08) < 1e-9
    finally:
        del os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"]


def test_policy_env_override_garbage_falls_back():
    os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"] = "not-a-number"
    try:
        p = RiskPolicy.from_env()
        # Falls back to default (or profile-adjusted default).
        assert 0 < p.daily_loss_kill_pct < 1
    finally:
        del os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"]


def test_policy_env_override_clamps_to_unit_interval():
    # 500 raw is nonsense and clamps to 1.0 (100%).
    os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"] = "500"
    try:
        p = RiskPolicy.from_env()
        assert p.daily_loss_kill_pct == 1.0
    finally:
        del os.environ["POLYMAKER_DAILY_LOSS_KILL_PCT"]


def test_policy_conservative_profile_shrinks_caps():
    p = RiskPolicy.from_env("conservative")
    # Loss kill is 0.5x default.
    expected = 0.5 * DEFAULT_DAILY_LOSS_KILL_PCT
    assert abs(p.daily_loss_kill_pct - expected) < 1e-9
    # Per-market cap is also 0.5x default.
    expected_market = 0.5 * DEFAULT_MAX_PER_MARKET_PCT
    assert abs(p.max_per_market_pct - expected_market) < 1e-9


def test_policy_aggressive_profile_grows_caps():
    p = RiskPolicy.from_env("aggressive")
    expected = 2.0 * DEFAULT_DAILY_LOSS_KILL_PCT
    assert abs(p.daily_loss_kill_pct - expected) < 1e-9


def test_policy_max_markets_override():
    os.environ["POLYMAKER_MAX_MARKETS"] = "3"
    try:
        p = RiskPolicy.from_env("balanced")
        assert p.max_concurrent_markets == 3
    finally:
        del os.environ["POLYMAKER_MAX_MARKETS"]


# ── ResolvedPolicy ──────────────────────────────────────────────────


def test_resolve_zero_capital_returns_zeros():
    p = RiskPolicy()
    r = p.resolve(0)
    assert r.capital_usdc == 0.0
    assert r.max_per_market_usdc == 0.0
    assert r.daily_loss_kill_usdc == 0.0


def test_resolve_rejects_negative_capital():
    p = RiskPolicy()
    with pytest.raises(ValueError):
        p.resolve(-100)


def test_resolve_pct_to_usdc():
    p = RiskPolicy(
        max_per_market_pct=0.05,
        total_exposure_pct=1.0,
        daily_loss_kill_pct=0.10,
        max_drawdown_kill_pct=0.25,
        per_market_loss_pct=0.05,
        per_trade_loss_pct=0.005,
        min_reward_pct_per_day=0.005,
    )
    r = p.resolve(capital_usdc=1000.0)
    assert r.capital_usdc == 1000.0
    assert abs(r.max_per_market_usdc - 50.0) < 1e-9
    assert abs(r.total_exposure_usdc - 1000.0) < 1e-9
    assert abs(r.daily_loss_kill_usdc - 100.0) < 1e-9
    assert abs(r.max_drawdown_kill_usdc - 250.0) < 1e-9
    assert abs(r.per_market_loss_usdc - 50.0) < 1e-9
    assert abs(r.per_trade_loss_usdc - 5.0) < 1e-9
    assert abs(r.min_reward_per_day_usdc - 5.0) < 1e-9


def test_resolve_scales_with_capital():
    p = RiskPolicy(daily_loss_kill_pct=0.10)
    r100 = p.resolve(100.0)
    r1000 = p.resolve(1000.0)
    assert abs(r1000.daily_loss_kill_usdc / r100.daily_loss_kill_usdc - 10.0) < 1e-9


def test_resolve_scale_to():
    p = RiskPolicy(daily_loss_kill_pct=0.10)
    r = p.resolve(100.0)
    r2 = r.scale_to(200.0)
    assert r2.capital_usdc == 200.0
    assert abs(r2.daily_loss_kill_usdc - 20.0) < 1e-9


def test_resolved_policy_back_reference():
    p = RiskPolicy(profile_name="aggressive")
    r = p.resolve(500.0)
    assert r.policy is p
    assert r.policy.profile_name == "aggressive"


# ── load_capital_usdc ───────────────────────────────────────────────


def test_load_capital_usdc_v3_env():
    os.environ["POLYMAKER_CAPITAL_USDC"] = "750.5"
    try:
        assert load_capital_usdc() == 750.5
    finally:
        del os.environ["POLYMAKER_CAPITAL_USDC"]


def test_load_capital_usdc_legacy_alias():
    os.environ["POLYMAKER_BANKROLL_USDC"] = "200"
    try:
        assert load_capital_usdc() == 200.0
    finally:
        del os.environ["POLYMAKER_BANKROLL_USDC"]


def test_load_capital_usdc_v3_wins_over_legacy():
    os.environ["POLYMAKER_CAPITAL_USDC"] = "500"
    os.environ["POLYMAKER_BANKROLL_USDC"] = "200"
    try:
        assert load_capital_usdc() == 500.0
    finally:
        del os.environ["POLYMAKER_CAPITAL_USDC"]
        del os.environ["POLYMAKER_BANKROLL_USDC"]


def test_load_capital_usdc_missing_returns_zero():
    os.environ.pop("POLYMAKER_CAPITAL_USDC", None)
    os.environ.pop("POLYMAKER_BANKROLL_USDC", None)
    assert load_capital_usdc() == 0.0


def test_load_capital_usdc_garbage_returns_zero():
    os.environ["POLYMAKER_CAPITAL_USDC"] = "not-a-number"
    try:
        assert load_capital_usdc() == 0.0
    finally:
        del os.environ["POLYMAKER_CAPITAL_USDC"]


def test_load_capital_usdc_negative_clamped():
    os.environ["POLYMAKER_CAPITAL_USDC"] = "-100"
    try:
        assert load_capital_usdc() == 0.0
    finally:
        del os.environ["POLYMAKER_CAPITAL_USDC"]
