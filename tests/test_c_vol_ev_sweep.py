"""Smoke for c_vol EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_c_vol_default():
    assert StrategyProfile().c_vol == 1.2
