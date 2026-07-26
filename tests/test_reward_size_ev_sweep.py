"""Smoke for reward_size EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_reward_size_mult_default():
    assert StrategyProfile().reward_size_mult == 1.0
