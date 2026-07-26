"""Smoke for q_max EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_q_max_usdc_default():
    assert StrategyProfile().q_max_usdc == 500.0
