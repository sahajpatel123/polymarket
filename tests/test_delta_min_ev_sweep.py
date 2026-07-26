"""Smoke for delta_min EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_delta_min_ticks_default():
    assert StrategyProfile().delta_min_ticks == 2
