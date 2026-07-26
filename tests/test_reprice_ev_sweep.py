"""Smoke for reprice_ticks EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_reprice_ticks_default():
    assert StrategyProfile().reprice_ticks == 2
