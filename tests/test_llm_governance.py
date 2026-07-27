"""Tests for V3 LLM governance safeguards.

The LLM is a nudger, not a steerer. These six rules (plus two new gates
in the verdict-driven refactor) are non-negotiable and must never be
bypassed:

1. Positive allowlist (SAFE_KNOBS) — LLM may only touch whitelisted
   knobs. Anything else is in ``rejected_keys`` and dropped.
2. LLM size multiplier ≤ 0.5, scaled by confidence (calibrated).
3. Dead-LLM timer (5s default).
4. Daily-loss kill is non-negotiable; tracked separately.
5. LLM reasoning log is append-only.
6. No directional bets from the LLM.
7. Reward-eligibility gate: LLM cannot "select" a market whose
   per-market cap can't fund ``rewardsMinSize × price``.
8. Paper-promotion gate: LLM-suggested knobs are paper_required.
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
    DEFAULT_PAPER_SECONDS,
    FORBIDDEN_LLM_PARAMS,
    SAFE_KNOB_RANGES,
    SAFE_KNOBS,
    LLMDailyLoss,
    LLMGovernance,
    RewardEligibility,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _gov(
    tmp_path: Path,
    capital: float = 1000.0,
    llm_size_mult: float = DEFAULT_LLM_SIZE_MULT,
    timeout: float = DEFAULT_DEAD_LLM_TIMEOUT_S,
    kill_pct: float = DEFAULT_LLM_DAILY_LOSS_PCT,
    paper_seconds: float = DEFAULT_PAPER_SECONDS,
) -> LLMGovernance:
    g = LLMGovernance(
        capital_usdc=capital,
        log_path=tmp_path / "llm_reasoning.jsonl",
        llm_size_mult=llm_size_mult,
        dead_llm_timeout_s=timeout,
        paper_seconds=paper_seconds,
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
    assert g.paper_seconds == DEFAULT_PAPER_SECONDS
    assert g.daily_loss.capital_usdc == 1000.0
    assert g.daily_loss.halted is False


def test_governance_rejects_oversized_llm_mult(tmp_path):
    with pytest.raises(ValueError):
        LLMGovernance(
            capital_usdc=1000.0,
            log_path=tmp_path / "x.jsonl",
            llm_size_mult=1.5,
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


def test_governance_rejects_negative_paper_seconds(tmp_path):
    with pytest.raises(ValueError):
        LLMGovernance(
            capital_usdc=1000.0,
            log_path=tmp_path / "x.jsonl",
            paper_seconds=-1.0,
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


def test_allows_safe_knob_passes(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"spread_mult": 1.2, "c_tox": 0.5}}
    d = g.check_and_log("p", response, _now(), confidence=0.8)
    assert d.approved is True
    assert d.actions.get("spread_mult") == 1.2
    assert d.actions.get("c_tox") == 0.5


# ── Rule 1: positive allowlist (SAFE_KNOBS) ─────────────────────────


def test_unknown_knob_rejected_not_silently_stripped(tmp_path):
    """The verdict: allowlist is primary; unknown keys go to rejected_keys."""
    g = _gov(tmp_path)
    response = {"actions": {"made_up_knob": 1.0, "spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "made_up_knob" in d.rejected_keys
    assert "made_up_knob" not in d.actions
    assert d.actions.get("spread_mult") == 1.1


def test_signature_type_outside_safe_set(tmp_path):
    """A known-bad knob is not in SAFE_KNOBS so it's rejected (not stripped)."""
    g = _gov(tmp_path)
    response = {"actions": {"signature_type": 0, "spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "signature_type" in d.rejected_keys
    assert "signature_type" not in d.actions


def test_kill_switch_outside_safe_set(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"kill_switch": False, "spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert "kill_switch" in d.rejected_keys


def test_safe_knobs_set_is_comprehensive():
    """All the SAFE_IMMEDIATE_KEYS from self_improve are mirrored here."""
    expected = {
        "delta_min_ticks", "c_vol", "c_tox", "c_kyle", "gamma",
        "min_edge_ticks", "layer_step_ticks", "layers",
        "reprice_ticks", "resize_frac", "flow_fv_weight",
        "trend_flow_z", "trend_vol_ratio",
        "event_jump_ticks", "event_cooloff_s",
        "event_sweep_mult", "event_sweep_frac",
        "join_best_bid",
    }
    assert expected.issubset(SAFE_KNOBS)


def test_risk_caps_in_forbidden_backstop():
    """Even though they're rejected by the allowlist, they're also in the
    forbidden backstop for defense in depth."""
    must_be_present = {
        "signature_type", "kill_switch", "post_only", "risk_profile",
        "bankroll_usdc", "POLYMAKER_CAPITAL_USDC", "daily_loss_kill_pct",
        "max_drawdown_kill_pct", "max_total_exposure_usdc",
        "max_market_notional_usdc", "max_open_orders_per_market",
        "max_position", "heartbeat",
    }
    for k in must_be_present:
        assert k in FORBIDDEN_LLM_PARAMS


# ── Rule 2: calibrated size cap (confidence-scaled) ──────────────────


def test_size_pct_zero_when_no_confidence(tmp_path):
    """Confidence=0 → size_pct=0 regardless of what LLM asked."""
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.9}}
    d = g.check_and_log("p", response, _now(), confidence=0.0)
    assert d.approved is True
    # size_pct should be 0 because confidence is 0.
    assert d.size_pct_after_cap == 0.0
    assert d.actions["size_pct"] == 0.0


def test_size_pct_capped_at_llm_mult(tmp_path):
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.9}}
    d = g.check_and_log("p", response, _now(), confidence=1.0)
    # With full confidence and cap=0.5, size_pct = min(0.9, 0.5, 0.5) = 0.5
    assert d.size_pct_after_cap == 0.5
    assert d.actions["size_pct"] == 0.5


def test_size_pct_scales_with_confidence(tmp_path):
    """size_pct = llm_size_mult * confidence (calibrated)."""
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.9}}
    d = g.check_and_log("p", response, _now(), confidence=0.6)
    # min(0.9, 0.5, 0.5*0.6) = min(0.9, 0.5, 0.3) = 0.3
    assert d.size_pct_after_cap == pytest.approx(0.3, abs=1e-9)


def test_size_pct_below_cap_passes_through(tmp_path):
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.2}}
    d = g.check_and_log("p", response, _now(), confidence=1.0)
    # min(0.2, 0.5, 0.5) = 0.2
    assert d.size_pct_after_cap == 0.2


def test_size_pct_garbage_dropped(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"size_pct": "not-a-number"}}
    d = g.check_and_log("p", response, _now(), confidence=0.5)
    assert d.approved is True
    # Garbage value gets dropped from actions (clamp_ranges).
    assert "size_pct" not in d.actions


def test_spread_mult_hard_capped(tmp_path):
    """spread_mult is clamped to [0.5, 3.0]."""
    g = _gov(tmp_path)
    response = {"actions": {"spread_mult": 10.0}}  # ask for 10x
    d = g.check_and_log("p", response, _now(), confidence=1.0)
    assert d.approved is True
    assert d.actions["spread_mult"] == 3.0
    assert "spread_mult" in d.clamped_keys


# ── Rule 3: dead-LLM timer ──────────────────────────────────────────


def test_dead_llm_triggers_fallback(tmp_path):
    g = _gov(tmp_path, timeout=0.5)
    started = _now() - 1.0
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, started)
    assert d.approved is False
    assert d.fallback_to_deterministic is True
    assert "dead_llm" in d.rejection_reason


def test_fast_llm_passes_through(tmp_path):
    g = _gov(tmp_path, timeout=5.0)
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert d.fallback_to_deterministic is False


# ── Rule 4: daily LLM loss separate from total ──────────────────────


def test_daily_loss_kills_llm(tmp_path):
    g = _gov(tmp_path, capital=1000.0, kill_pct=0.05)
    g.record_llm_fill(10.0)
    g.record_llm_fill(20.0)
    g.record_llm_fill(-100.0)
    assert g.daily_loss.halted is True
    assert g.daily_loss.halt_reason
    d = g.check_and_log("p", {"actions": {"spread_mult": 1.1}}, _now())
    assert d.approved is False
    assert "llm_daily_loss" in d.rejection_reason


def test_daily_loss_does_not_halt_below_threshold(tmp_path):
    g = _gov(tmp_path, capital=1000.0, kill_pct=0.05)
    g.record_llm_fill(-30.0)
    assert g.daily_loss.halted is False


def test_daily_loss_reset_clears_halt(tmp_path):
    g = _gov(tmp_path, capital=1000.0, kill_pct=0.05)
    g.record_llm_fill(-100.0)
    assert g.daily_loss.halted is True
    g.daily_loss.reset()
    assert g.daily_loss.halted is False
    assert g.daily_loss.day_pnl_usdc == 0.0


def test_daily_loss_kill_threshold_percent():
    g = LLMDailyLoss(capital_usdc=1000.0)
    g.set_kill_pct(0.05)
    g.record_fill(-49.0)
    assert g.halted is False
    g.record_fill(-2.0)
    assert g.halted is True


# ── Rule 5: reasoning log ───────────────────────────────────────────


def test_log_file_created(tmp_path):
    g = _gov(tmp_path)
    g.check_and_log("test prompt", {"actions": {"spread_mult": 1.0}}, _now())
    assert g.log_path.exists()


def test_log_records_decision(tmp_path):
    g = _gov(tmp_path)
    g.check_and_log("p1", {"actions": {"spread_mult": 1.5}}, _now(), confidence=0.5)
    g.check_and_log("p2", {"actions": {"side": "BUY"}}, _now())
    with g.log_path.open() as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert len(rows) == 2
    assert rows[0]["approved"] is True
    assert rows[0]["actions"]["spread_mult"] == 1.5
    assert rows[1]["approved"] is False
    assert "side" in rows[1]["stripped_fields"]


def test_log_records_new_fields(tmp_path):
    g = _gov(tmp_path, paper_seconds=1800)
    g.check_and_log("p", {"actions": {"spread_mult": 1.1}}, _now(), confidence=0.7)
    with g.log_path.open() as fh:
        row = json.loads(fh.readline())
    assert row["paper_required"] is True
    assert row["paper_seconds"] == 1800
    assert row["confidence"] == 0.7


def test_log_records_latency(tmp_path):
    g = _gov(tmp_path)
    started = _now() - 0.1
    g.check_and_log("p", {"actions": {}}, started)
    with g.log_path.open() as fh:
        row = json.loads(fh.readline())
    assert row["latency_ms"] >= 100
    assert row["latency_ms"] < 5000


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


# ── Rule 7: reward eligibility gate ─────────────────────────────────


def test_reward_eligibility_pass():
    e = RewardEligibility.check(
        condition_id="0x1",
        rewards_min_size=200,
        typical_price=0.20,
        per_market_cap_usdc=50.0,
    )
    assert e.eligible is True
    assert e.min_order_notional_usdc == 40.0
    assert e.shortfall_usdc == 0.0


def test_reward_eligibility_fail():
    """A $25 cap can't fund a 200-share order at $0.20 = $40 min."""
    e = RewardEligibility.check(
        condition_id="0x1",
        rewards_min_size=200,
        typical_price=0.20,
        per_market_cap_usdc=25.0,
    )
    assert e.eligible is False
    assert e.min_order_notional_usdc == 40.0
    assert e.shortfall_usdc == 15.0


def test_selection_with_undercapitalized_market_rejected(tmp_path):
    g = _gov(tmp_path)
    context = {
        "kind": "market_selection",
        "condition_id": "0xabc",
        "rewards_min_size": 200,
        "typical_price": 0.20,
        "per_market_cap_usdc": 25.0,  # under $40 min
    }
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now(), context=context)
    assert d.approved is False
    assert "not_reward_eligible" in d.rejection_reason
    assert d.reward_eligibility is not None
    assert d.reward_eligibility.eligible is False


def test_selection_with_adequately_capitalized_market_approved(tmp_path):
    g = _gov(tmp_path)
    context = {
        "kind": "market_selection",
        "condition_id": "0xabc",
        "rewards_min_size": 200,
        "typical_price": 0.20,
        "per_market_cap_usdc": 50.0,
    }
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now(), context=context)
    assert d.approved is True
    assert d.reward_eligibility is not None
    assert d.reward_eligibility.eligible is True


def test_non_selection_call_skips_eligibility_check(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now(), context={"kind": "commentary"})
    assert d.approved is True
    assert d.reward_eligibility is None


# ── Rule 8: paper-promotion gate ────────────────────────────────────


def test_paper_required_when_actions_present(tmp_path):
    g = _gov(tmp_path, paper_seconds=3600)
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert d.paper_required is True


def test_no_paper_required_when_no_actions(tmp_path):
    g = _gov(tmp_path, paper_seconds=3600)
    response = {"actions": {}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert d.paper_required is False


def test_no_paper_required_when_paper_seconds_zero(tmp_path):
    g = _gov(tmp_path, paper_seconds=0)
    response = {"actions": {"spread_mult": 1.1}}
    d = g.check_and_log("p", response, _now())
    assert d.approved is True
    assert d.paper_required is False


# ── Numeric range clamping ──────────────────────────────────────────


def test_safe_knob_ranges_complete():
    """Every SAFE_KNOB has a numeric range (defense in depth)."""
    for knob in SAFE_KNOBS:
        assert knob in SAFE_KNOB_RANGES, f"missing range for {knob}"


def test_numeric_clamping_for_safe_knob(tmp_path):
    g = _gov(tmp_path)
    # layers is clamped to [1, 8]; ask for 100.
    response = {"actions": {"layers": 100}}
    d = g.check_and_log("p", response, _now(), confidence=0.5)
    assert d.approved is True
    assert d.actions["layers"] == 8
    assert "layers" in d.clamped_keys


def test_non_numeric_knob_value_dropped(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"layers": "many"}}
    d = g.check_and_log("p", response, _now(), confidence=0.5)
    assert d.approved is True
    # Non-numeric value is dropped; the key is in clamped_keys.
    assert "layers" not in d.actions
    assert "layers" in d.clamped_keys


# ── Confidence clamping ─────────────────────────────────────────────


def test_confidence_above_1_clamped_to_1(tmp_path):
    g = _gov(tmp_path, llm_size_mult=0.5)
    response = {"actions": {"size_pct": 0.9}}
    d = g.check_and_log("p", response, _now(), confidence=2.0)
    # confidence clamped to 1.0, so size_pct = min(0.9, 0.5, 0.5) = 0.5
    assert d.confidence == 1.0
    assert d.size_pct_after_cap == 0.5


def test_confidence_negative_clamped_to_0(tmp_path):
    g = _gov(tmp_path)
    response = {"actions": {"size_pct": 0.9}}
    d = g.check_and_log("p", response, _now(), confidence=-0.5)
    assert d.confidence == 0.0
    assert d.size_pct_after_cap == 0.0


# ── Edge cases ──────────────────────────────────────────────────────


def test_non_dict_response_safely_approved(tmp_path):
    g = _gov(tmp_path)
    d = g.check_and_log("p", "just a plain string response", _now())
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
    g = _gov(tmp_path)
    d = g.check_and_log("p", {"actions": {"spread_mult": 1.0}}, _now())
    with pytest.raises((AttributeError, Exception)):
        d.approved = False  # type: ignore[misc]


# ── Adversarial critic hook ─────────────────────────────────────────


def test_critique_prompt_returns_string(tmp_path):
    g = _gov(tmp_path)
    p = g.critique_prompt(
        suggestion="widen spread to 2x",
        actions={"spread_mult": 2.0},
        context={"cid": "0xabc"},
    )
    assert isinstance(p, str)
    assert len(p) > 50
    # The prompt must be advisory only; it cannot override governance.
    assert "ADVISORY" in p or "CANNOT" in p
    # And it must restate the risk policy.
    assert "NON-NEGOTIABLE" in p or "non-negotiable" in p


def test_critique_prompt_includes_suggestion_and_actions(tmp_path):
    g = _gov(tmp_path)
    p = g.critique_prompt(
        suggestion="unique-marker-xyz",
        actions={"spread_mult": 1.5, "size_pct": 0.3},
    )
    assert "unique-marker-xyz" in p
    assert "spread_mult" in p
    assert "size_pct" in p
