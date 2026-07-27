"""Tests for V3 LLM governance safeguards.

The LLM is a nudger, not a steerer. These six rules are non-negotiable
and must never be bypassed:

1. LLM output passes the same risk gates as human quoting.
2. LLM size multiplier ≤ 0.5.
3. Dead-LLM timer (5s default).
4. Daily-loss kill is non-negotiable; tracked separately.
5. LLM reasoning log is append-only.
6. No directional bets from the LLM.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from polymaker.intelligence.llm_governance import (
    DEFAULT_DEAD_LLM_TIMEOUT_S,
    DEFAULT_LLM_DAILY_LOSS_PCT,
    DEFAULT_LLM_SIZE_MULT,
    FORBIDDEN_LLM_PARAMS,
    LLMDailyLoss,
    LLMGovernance,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _gov(
    tmp_path: Path,
    capital: float = 1000.0,
    llm_size_mult: float = DEFAULT_LLM_SIZE_MULT,
    timeout: float = DEFAULT_DEAD_LLM_TIMEOUT_S,
    kill_pct: float = DEFAULT_LLM_DAILY_LOSS_PCT,
) -> LLMGovernance:
    g = LLMGovernance(
        capital_usdc=capital,
        log_path=tmp_path / "llm_reasoning.jsonl",
        llm_size_mult=llm_size_mult,
        dead_llm_timeout_s=timeout,
    )
    g.daily_loss._kill_pct = kill_pct
    return g


def _now() -> float:
    return time.time()


# ── Construction ─────────────────────────────────────────────────────


def test_governance_default_construction(tmp_path):
    g = _gov(tmp_path)
    assert g.capital_usdc == 1000.0
    assert g.llm_size_mult == DEFAULT_LLM_SIZE_MULT
    assert g.dead_llm_timeout_s == DEFAULT_DEAD_LLM_TIMEOUT_S
    assert g.daily_loss.capital_usdc == 1000.0
    assert g.daily_loss.halted is False


def test_governance_rejects_oversized_llm_mult(tmp_path):
    with pytest.raises(ValueError):
        LLMGovernance(
            capital_usdc=1000.0,
            log_path=tmp_path / "x.jsonl",
            llm_size_mult=1.5,  # > 1
        )


def test_governance_rejects_zero_llm_mult(tmp_path):
    with pytest.raises(ValueError):
        LLMGovernance(
            capital_usdc=1000.0,
            log_path=tmp_path / "x.jsonl",
            llm_size_mult=0.0,
        )


def test_governance_rejects_zero_timeout(tmp_path):
    with pytest.raises(ValueError):
        LLMGovernance(
            capital_usdc=1000.0,
            log_path=tmp_path / "x.jsonl",
            dead_llm_timeout_s=0.0,
        )


# ── Rule 6: no directional bets ─────────────────────────────────────


def test_rejects_directional_side_field(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"side": "BUY_YES"}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is False
    assert "side" in d.stripped_fields
    assert "directional" in d.rejection_reason.lower() or "side" in d.rejection_reason


def test_rejects_direction_field(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"direction": "long"}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is False
    assert "direction" in d.stripped_fields


def test_rejects_buy_this_market(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"buy_this_market": "0xabc"}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is False
    assert "buy_this_market" in d.stripped_fields


def test_allows_parameter_nudges(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"spread_mult": 1.2, "size_pct": 0.05}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert d.actions.get("spread_mult") == 1.2


# ── Rule 2: LLM size cap ≤ 0.5 ─────────────────────────────────────


def test_size_pct_capped_at_llm_mult(tmp_path):
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.9}}  # LLM asks for 90%
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    # Capped to 0.5 (default llm_size_mult).
    assert d.actions["size_pct"] == 0.5


def test_size_pct_below_cap_passes_through(tmp_path):
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.3}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert d.actions["size_pct"] == 0.3


def test_size_pct_custom_cap(tmp_path):
    g = _gov(tmp_path, llm_size_mult=0.3)
    response = {"actions": {"size_pct": 0.8}}
    d = g.check_and_log("p", response, _now())
    assert d.actions["size_pct"] == 0.3


def test_size_pct_garbage_dropped(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"size_pct": "not-a-number"}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "size_pct" not in d.actions


# ── Rule 1: same risk gates ─────────────────────────────────────────


def test_forbidden_param_daily_loss_kill_rejected(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"daily_loss_kill_pct": 0.5}}  # LLM tries to relax
    d = g.check_and_log("p", response, _now())
    assert d.approved is False
    assert "daily_loss_kill_pct" in d.stripped_keys
    assert "risk_cap" in d.rejection_reason or "risk" in d.rejection_reason.lower()


def test_forbidden_param_max_position_rejected(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"max_position": 1000.0}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is False
    assert "max_position" in d.stripped_keys


def test_forbidden_param_signature_type_stripped(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"signature_type": 0}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "signature_type" in d.stripped_keys
    assert "signature_type" not in d.actions


def test_forbidden_param_kill_switch_stripped(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"kill_switch": False}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "kill_switch" in d.stripped_keys


def test_forbidden_param_set_is_comprehensive():
    """All the big risk knobs are in the forbidden set."""
    must_be_present = {
        "signature_type", "kill_switch", "post_only", "risk_profile",
        "bankroll_usdc", "POLYMAKER_CAPITAL_USDC", "daily_loss_kill_pct",
        "max_drawdown_kill_pct", "max_open_orders_per_market", "heartbeat",
    }
    for k in must_be_present:
        assert k in FORBIDDEN_LLM_PARAMS


# ── Rule 3: dead-LLM timer ──────────────────────────────────────────


def test_dead_llm_triggers_fallback(tmp_path):
    g = _gov(tmp_path, timeout=0.5)
    started = _now() - 1.0  # 1s ago, > 0.5s timeout
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, started)
    assert d.approved is False
    assert d.fallback_to_deterministic is True
    assert "dead_llm" in d.rejection_reason


def test_fast_llm_passes_through(tmp_path):
    g = _gov(tmp_path, timeout=5.0)
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())  # started now → instant
    assert d.approved is True
    assert d.fallback_to_deterministic is False


# ── Rule 4: daily LLM loss separate from total ──────────────────────


def test_daily_loss_kills_llm(tmp_path):
    g = _gov(tmp_path, capital=1000.0, kill_pct=0.05)
    g.daily_loss._kill_pct = 0.05  # 5% of 1000 = $50
    # Three small wins, then a big loss.
    g.record_llm_fill(10.0)
    g.record_llm_fill(20.0)
    g.record_llm_fill(-100.0)  # -$70 day → halt
    assert g.daily_loss.halted is True
    assert g.daily_loss.halt_reason
    # New LLM calls now rejected.
    d = g.check_and_log("p", {"actions": {"spread_mult": 1.1}}, _now())
    assert d.approved is False
    assert "llm_daily_loss" in d.rejection_reason


def test_daily_loss_does_not_halt_below_threshold(tmp_path):
    g = _gov(tmp_path, capital=1000.0, kill_pct=0.05)
    g.record_llm_fill(-30.0)  # 3% < 5% threshold
    assert g.daily_loss.halted is False


def test_daily_loss_reset_clears_halt(tmp_path):
    g = _gov(tmp_path, capital=1000.0, kill_pct=0.05)
    g.record_llm_fill(-100.0)
    assert g.daily_loss.halted is True
    g.daily_loss.reset()
    assert g.daily_loss.halted is False
    assert g.daily_loss.day_pnl_usdc == 0.0


def test_daily_loss_kill_threshold_percent():
    """The 5% default means $50 on a $1000 capital."""
    g = LLMDailyLoss(capital_usdc=1000.0)
    g.set_kill_pct(0.05)
    g.record_fill(-49.0)  # under
    assert g.halted is False
    g.record_fill(-2.0)  # cumulative -51 → over
    assert g.halted is True


# ── Rule 5: reasoning log ───────────────────────────────────────────


def test_log_file_created(tmp_path):
    g = _gov(tmp_path)
    g.check_and_log("test prompt", {"actions": {"spread_mult": 1.0}}, _now())
    assert g.log_path.exists()


def test_log_records_decision(tmp_path):
    g = _gov(tmp_path)
    g.check_and_log("p1", {"actions": {"spread_mult": 1.5}}, _now())
    g.check_and_log("p2", {"actions": {"side": "BUY"}}, _now())
    with g.log_path.open() as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert len(rows) == 2
    assert rows[0]["approved"] is True
    assert rows[0]["actions"]["spread_mult"] == 1.5
    assert rows[1]["approved"] is False
    assert "side" in rows[1]["stripped_fields"]


def test_log_records_latency(tmp_path):
    g = _gov(tmp_path)
    started = _now() - 0.1  # 100ms ago
    g.check_and_log("p", {"actions": {}}, started)
    with g.log_path.open() as fh:
        row = json.loads(fh.readline())
    assert row["latency_ms"] >= 100
    assert row["latency_ms"] < 5000  # within timeout


def test_log_records_reasoning_id_monotonic(tmp_path):
    g = _gov(tmp_path)
    g.check_and_log("p", {"actions": {}}, _now())
    g.check_and_log("p", {"actions": {}}, _now())
    g.check_and_log("p", {"actions": {}}, _now())
    with g.log_path.open() as fh:
        ids = [json.loads(line)["reasoning_id"] for line in fh if line.strip()]
    assert ids == [1, 2, 3]


def test_log_truncates_huge_prompts(tmp_path):
    g = _gov(tmp_path)
    big = "x" * 10_000
    g.check_and_log(big, {"actions": {}}, _now())
    with g.log_path.open() as fh:
        row = json.loads(fh.readline())
    assert len(row["prompt"]) <= 2000


# ── Rule 1 (continued): strip happens before approve ────────────────


def test_stripped_forbidden_keys_listed(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"signature_type": 0, "post_only": False, "spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "signature_type" in d.stripped_keys
    assert "post_only" in d.stripped_keys
    assert "spread_mult" in d.actions


# ── Edge cases ──────────────────────────────────────────────────────


def test_non_dict_response_safely_approved(tmp_path):
    g = _gov(tmp_path)
    d = g.check_and_log("p", "just a plain string response", _now())
    # No actions found, so no rejection. Falls through to approved.
    assert d.approved is True
    assert d.actions == {}


def test_list_response_safely_handled(tmp_path):
    g = _gov(tmp_path)
    d = g.check_and_log("p", [{"text": "hello"}], _now())
    assert d.approved is True


def test_empty_actions_approved(tmp_path):
    g = _gov(tmp_path)
    d = g.check_and_log("p", {"actions": {}}, _now())
    assert d.approved is True


def test_governance_decision_is_frozen(tmp_path):
    """GovernanceDecision is immutable."""
    g = _gov(tmp_path)
    d = g.check_and_log("p", {"actions": {"spread_mult": 1.0}}, _now())
    with pytest.raises((AttributeError, Exception)):
        d.approved = False  # type: ignore[misc]
