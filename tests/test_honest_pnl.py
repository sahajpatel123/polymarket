"""Honest PnL: monopoly rewards cannot pass as financial edge."""

from __future__ import annotations

import json
from pathlib import Path

from polymaker.metrics.analyze import analyze
from polymaker.metrics.honest_pnl import (
    REWARD_SHARE_CONSERVATIVE,
    compute_honest_pnl,
    honest_pnl_from_report,
)


def test_undersized_in_band_does_not_earn_full_pool() -> None:
    h = compute_honest_pnl(
        instant_spread_usdc=1.0,
        rewards_daily_rate=200.0,
        eligible_in_band_seconds=0.0,  # all undersized
        undersized_in_band_seconds=80_000.0,
        monopoly_reward_usdc=39.0,  # analyzer monopoly diagnostic
        n_fill=50,
        n_quote=100,
    )
    assert h.reward_conservative_usdc == 0.0
    assert h.reward_base_usdc == 0.0
    assert h.monopoly_reward_usdc == 39.0  # diagnostic still present
    assert h.pnl_without_rewards_usdc == 1.0
    assert "mostly_undersized_in_band_quotes" in h.claim_blockers or not h.financial_claim_ok


def test_as_markout_haircut_lowers_net() -> None:
    h = compute_honest_pnl(
        instant_spread_usdc=10.0,
        markout_30s_mean=-0.02,
        markout_n=50,
        total_fill_shares=100.0,
        n_fill=50,
        n_quote=200,
        rewards_daily_rate=0.0,
        eligible_in_band_seconds=0.0,
    )
    assert h.as_adjusted_spread_usdc < h.instant_spread_usdc
    assert abs(h.as_adjusted_spread_usdc - (10.0 - 2.0)) < 1e-9
    assert h.pnl_without_rewards_usdc == h.as_adjusted_spread_usdc


def test_monopoly_only_positive_blocked() -> None:
    h = compute_honest_pnl(
        instant_spread_usdc=-5.0,
        markout_30s_mean=0.0,
        markout_n=10,
        total_fill_shares=10.0,
        n_fill=50,
        n_quote=200,
        rewards_daily_rate=200.0,
        eligible_in_band_seconds=0.0,
        monopoly_reward_usdc=40.0,
    )
    assert h.pnl_without_rewards_usdc <= 0
    assert h.pnl_monopoly_diagnostic_usdc > 0
    assert "monopoly_rewards_only_positive" in h.claim_blockers
    assert h.financial_claim_ok is False


def test_conservative_reward_less_than_monopoly() -> None:
    h = compute_honest_pnl(
        instant_spread_usdc=2.0,
        n_fill=50,
        n_quote=200,
        rewards_daily_rate=200.0,
        eligible_in_band_seconds=43_200.0,  # 12h
        monopoly_reward_usdc=100.0,
    )
    eligible_pool = 200.0 * (43_200.0 / 86_400.0)
    assert abs(h.reward_conservative_usdc - eligible_pool * REWARD_SHARE_CONSERVATIVE) < 1e-9
    assert h.reward_conservative_usdc < h.monopoly_reward_usdc
    assert h.pnl_conservative_usdc < h.pnl_monopoly_diagnostic_usdc


def test_analyze_attaches_honest_pnl(tmp_path: Path) -> None:
    """Shipped analyze() path produces honest_pnl on MetricsReport."""
    t0 = 1_700_000_000.0
    rows = [
        {
            "ts": t0,
            "event": "market_meta",
            "condition_id": "c1",
            "rewards_daily_rate": 200.0,
            "rewards_max_spread": 5.0,
            "rewards_min_size": 10.0,
        },
        {
            "ts": t0 + 1,
            "event": "quote",
            "condition_id": "c1",
            "order_id": "o1",
            "token_id": "yes",
            "side": "BUY",
            "price": 0.48,
            "size": 3.0,  # undersized vs min 10
            "mid": 0.50,
            "in_reward_band": True,
        },
        {
            "ts": t0 + 100,
            "event": "mark",
            "condition_id": "c1",
            "fv": 0.51,
            "inventory_net": 0.0,
        },
        {
            "ts": t0 + 200,
            "event": "fill",
            "condition_id": "c1",
            "token_id": "yes",
            "side": "BUY",
            "price": 0.48,
            "size": 20.0,
            "mid": 0.50,
        },
        {
            "ts": t0 + 230,
            "event": "mark",
            "condition_id": "c1",
            "fv": 0.47,  # adverse for BUY
            "inventory_net": 20.0,
        },
        {
            "ts": t0 + 400,
            "event": "cancel",
            "condition_id": "c1",
            "order_id": "o1",
            "token_id": "yes",
            "side": "BUY",
            "price": 0.48,
            "size": 3.0,
        },
    ]
    path = tmp_path / "m.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    rep = analyze(path)
    assert rep.n_fill == 1
    assert rep.honest_pnl
    assert "pnl_without_rewards_usdc" in rep.honest_pnl
    assert "reward_conservative_usdc" in rep.honest_pnl
    assert "pnl_monopoly_diagnostic_usdc" in rep.honest_pnl
    # Instant spread = (0.50-0.48)*20 = 0.4
    assert abs(rep.realized_spread_usdc - 0.4) < 1e-6
    # Conservative rewards must be << monopoly diagnostic when undersized
    h = rep.honest_pnl
    assert h["reward_conservative_usdc"] <= h["monopoly_reward_usdc"] + 1e-9
