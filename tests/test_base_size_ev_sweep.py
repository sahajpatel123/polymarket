"""Smoke for base_size EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_base_size_usdc_default():
    assert StrategyProfile().base_size_usdc == 50.0
