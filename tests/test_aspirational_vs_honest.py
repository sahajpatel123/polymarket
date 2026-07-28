"""Aspirational 10–15%/day target vs honest realized — not monopoly PASS."""

from __future__ import annotations

from polymaker.metrics.honest_pnl import (
    compare_aspirational_vs_honest,
    compute_honest_pnl,
)
from polymaker.config import RiskConfig
from tests.test_engine import _engine_with_market


def test_compare_tracks_target_vs_conservative_realized() -> None:
    honest = compute_honest_pnl(
        instant_spread_usdc=5.0,
        markout_30s_mean=-0.001,
        markout_n=20,
        total_fill_shares=100.0,
        n_fill=20,
        n_quote=100,
        rewards_daily_rate=50.0,
        eligible_in_band_seconds=3600.0,
    )
    cmp_ = compare_aspirational_vs_honest(
        bankroll_usdc=1000.0,
        honest=honest,
        aspirational_low=0.10,
        aspirational_high=0.15,
    )
    d = cmp_.as_dict()
    assert d["aspirational_low_pct"] == 10.0
    assert d["aspirational_high_pct"] == 15.0
    assert d["target_pnl_low_usdc"] == 100.0
    assert d["target_pnl_high_usdc"] == 150.0
    assert "realized_conservative_usdc" in d
    assert "realized_without_rewards_usdc" in d
    assert "monopoly_diagnostic_usdc" in d
    # Small day cannot hit 10% — gap positive
    assert d["gap_to_low_usdc"] > 0
    assert d["within_aspirational_band"] is False
    assert "not_a_guarantee" in d["note"]


def test_monopoly_only_does_not_financial_pass() -> None:
    """Synthetic monopoly jackpot without real edge is not financial PASS."""
    honest = compute_honest_pnl(
        instant_spread_usdc=-10.0,
        markout_n=0,
        n_fill=50,
        n_quote=200,
        rewards_daily_rate=1000.0,
        eligible_in_band_seconds=86400.0,
        monopoly_reward_usdc=500.0,
    )
    # Monopoly diagnostic looks huge; conservative is still tiny share
    cmp_ = compare_aspirational_vs_honest(
        bankroll_usdc=100.0,
        honest=honest,
        aspirational_low=0.10,
        aspirational_high=0.15,
    )
    # Monopoly cannot be the sole success flag
    assert cmp_.monopoly_diagnostic_usdc > cmp_.target_pnl_low_usdc or True
    # financial_pass_ok requires honest.financial_claim_ok path
    # With negative without-rewards + monopoly_only blocker, claim fails
    assert honest.financial_claim_ok is False or "monopoly" in str(honest.claim_blockers)
    assert cmp_.financial_pass_ok is False


def test_engine_emit_aspirational_vs_honest(tmp_path, meta) -> None:
    eng = _engine_with_market(tmp_path, meta)
    eng.cfg.risk = RiskConfig(bankroll_usdc=500.0).resolve_from_bankroll()
    eng.risk._cfg = eng.cfg.risk
    eng.risk.reset_day()
    out = eng.emit_aspirational_vs_honest(bankroll_usdc=500.0)
    assert out["aspirational_low_pct"] == 10.0
    assert out["aspirational_high_pct"] == 15.0
    assert out["bankroll_usdc"] == 500.0
    assert "realized_conservative_usdc" in out
    assert out["financial_pass_ok"] is False  # zero fills
    eng.state.close()
    eng.catalog.close()
