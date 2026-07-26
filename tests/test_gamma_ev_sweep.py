"""Smoke tests for gamma EV sweep + weekly timeout helper."""

from __future__ import annotations

from polymaker.config import StrategyProfile
import scripts.write_weekly_report as wr


def test_gamma_default():
    assert StrategyProfile().gamma == 0.5


def test_weekly_run_timeout(monkeypatch):
    """_run must not hang forever — TimeoutExpired → status=TIMEOUT."""
    import subprocess

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["sleep"], timeout=0.01)

    monkeypatch.setattr(wr.subprocess, "run", boom)
    code, _out, err = wr._run(["sleep", "999"], timeout_s=0.01)
    assert code == 124
    assert "status=TIMEOUT" in err
