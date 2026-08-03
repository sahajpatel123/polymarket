"""The go-live preflight must stay green.

These are the invariants that make live trading survivable — the daily-loss stop
latching and surviving restarts, exits that are reachable and cannot be rejected
by post_only, paper/live state separation, and the governor being driven by money
rather than fair-value drift. Each one corresponds to a defect that was live in
this repo, so they are enforced in the suite rather than left to a manual run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from preflight_golive import (  # noqa: E402
    FAIL,
    Report,
    check_exit_path,
    check_governor,
    check_kill_switch,
    check_llm,
    check_mode_separation,
    check_risk,
)


def _names(rep: Report) -> dict[str, str]:
    return {c.name: c.status for c in rep.checks}


def test_kill_switch_checks_pass(tmp_path) -> None:
    rep = Report()
    check_kill_switch(rep, tmp_path)
    assert rep.failed == [], [c.__dict__ for c in rep.failed]
    st = _names(rep)
    assert st["kill.latches_through_recovery"] != FAIL
    assert st["kill.survives_restart"] != FAIL


def test_exit_path_checks_pass() -> None:
    rep = Report()
    check_exit_path(rep)
    assert rep.failed == [], [c.__dict__ for c in rep.failed]
    st = _names(rep)
    for k in ("exit.reachable", "exit.never_crosses_the_bid",
              "exit.holds_through_noise", "exit.never_oversells"):
        assert st[k] != FAIL, k


def test_mode_separation_checks_pass() -> None:
    rep = Report()
    check_mode_separation(rep)
    assert rep.failed == [], [c.__dict__ for c in rep.failed]


def test_governor_checks_pass() -> None:
    rep = Report()
    check_governor(rep)
    assert rep.failed == [], [c.__dict__ for c in rep.failed]


def test_llm_provider_checks_pass() -> None:
    rep = Report()
    check_llm(rep)
    assert rep.failed == [], [c.__dict__ for c in rep.failed]


@pytest.mark.parametrize("config_dir", ["config", "live"])
def test_shipped_configs_have_sane_risk_limits(config_dir: str) -> None:
    from polymaker.config import Config

    root = Path(__file__).resolve().parent.parent
    if not (root / config_dir / "config.toml").exists():
        pytest.skip(f"{config_dir}/config.toml not present")
    cfg = Config.load(str(root / config_dir))
    rep = Report()
    check_risk(rep, cfg)
    assert rep.failed == [], [c.__dict__ for c in rep.failed]


def test_preflight_exits_nonzero_when_a_check_fails() -> None:
    """The harness must be able to block a deploy, not just describe one."""
    rep = Report()
    rep.add("synthetic", False, "deliberate failure")
    assert rep.failed, "a failing check must be reported as FAIL"
    assert rep.failed[0].status == FAIL
