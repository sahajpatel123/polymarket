"""Config parsing edge cases (T1-05)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from polymaker.config import Config, MarketEntry, StrategyProfile


def test_load_missing_files_defaults(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path, load_env=False)
    assert cfg.markets == []
    assert cfg.profiles == {}
    assert cfg.wallet.chain_id == 137


def test_strategy_profile_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        StrategyProfile(not_a_real_knob=1)  # type: ignore[call-arg]


def test_market_entry_requires_identifier() -> None:
    with pytest.raises(ValidationError):
        MarketEntry(profile="x")


def test_profile_overrides_apply() -> None:
    base = StrategyProfile(base_size_usdc=50.0, gamma=0.5)
    over = base.with_overrides({"base_size_usdc": 12.0, "unknown_ignored": 99})
    assert over.base_size_usdc == 12.0
    assert over.gamma == 0.5


def test_enabled_markets_filter(tmp_path: Path) -> None:
    (tmp_path / "markets.toml").write_text(
        '[[markets]]\nslug="a"\nprofile="p"\nenabled=true\n'
        '[[markets]]\nslug="b"\nprofile="p"\nenabled=false\n'
    )
    (tmp_path / "strategy.toml").write_text("[profiles.p]\nbase_size_usdc=10\n")
    (tmp_path / "config.toml").write_text("")
    cfg = Config.load(tmp_path, load_env=False)
    assert [m.slug for m in cfg.enabled_markets] == ["a"]
    assert len(cfg.markets) == 2


def test_load_strategy_and_resolve_profile(tmp_path: Path) -> None:
    (tmp_path / "strategy.toml").write_text(
        "[profiles.demo]\nbase_size_usdc=33\nq_max_usdc=99\n"
    )
    (tmp_path / "markets.toml").write_text(
        '[[markets]]\nslug="s"\nprofile="demo"\nbase_size_usdc=7\nenabled=true\n'
    )
    (tmp_path / "config.toml").write_text("[risk]\ndaily_loss_kill_usdc=12\n")
    cfg = Config.load(tmp_path, load_env=False)
    assert cfg.risk.daily_loss_kill_usdc == 12
    p = cfg.profile_for(cfg.markets[0])
    assert p.base_size_usdc == 7
    assert p.q_max_usdc == 99
