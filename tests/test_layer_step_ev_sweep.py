"""Smoke for layer_step EV sweep defaults."""

from __future__ import annotations

from polymaker.config import StrategyProfile


def test_layer_step_ticks_default():
    assert StrategyProfile().layer_step_ticks == 2
