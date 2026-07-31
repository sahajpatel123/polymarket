"""Tests for self-improvement loop (mocked LLM)."""

from __future__ import annotations

from pathlib import Path

from polymaker.intelligence.profile_history import ProfileHistory
from polymaker.intelligence.self_eval import SelfEvaluation
from polymaker.intelligence.self_improve import (
    FORBIDDEN_KEYS,
    REASONING_MODEL,
    SAFE_IMMEDIATE_KEYS,
    SelfImprover,
    apply_overrides,
    build_trade_journal,
    needs_improvement,
    requires_paper,
)


def _decaying_eval(n: int = 60) -> SelfEvaluation:
    ev = SelfEvaluation()
    # Fill window with negative PnL to trip decay (threshold -0.5 Sharpe).
    for _ in range(n):
        ev.update(-1.0, "QUIET", "0")
    # Force consecutive periods high enough.
    while not ev.decay.is_decaying() and ev.decay.consecutive_periods < 10:
        ev.update(-1.0, "QUIET", "0")
    return ev


def _low_hit_rate_eval(n: int = 60) -> SelfEvaluation:
    ev = SelfEvaluation()
    for i in range(n):
        # ~30% hit rate
        pnl = 1.0 if i % 10 < 3 else -1.0
        ev.update(pnl, "QUIET", "0")
    return ev


def test_needs_improvement_decay_and_hit_rate() -> None:
    healthy = SelfEvaluation()
    assert needs_improvement(healthy) == (False, "healthy")

    low = _low_hit_rate_eval()
    ok, reason = needs_improvement(low)
    assert ok
    assert "hit_rate" in reason

    dec = _decaying_eval()
    ok2, reason2 = needs_improvement(dec)
    assert ok2
    assert reason2 == "strategy_decaying"


def test_build_trade_journal_limit() -> None:
    ev = SelfEvaluation()
    for i in range(300):
        ev.update(0.1 if i % 2 == 0 else -0.05, "TRENDING", str(i % 3))
    journal = build_trade_journal(ev, limit=200)
    assert len(journal) == 200
    assert "pnl" in journal[0]
    assert "regime" in journal[0]


def test_forbidden_keys_never_applied() -> None:
    base = {"gamma": 0.5, "q_max_usdc": 250.0, "base_size_usdc": 50.0}
    out = apply_overrides(
        base,
        {"gamma": 0.8, "q_max_usdc": 9999.0, "base_size_usdc": 1.0},
    )
    assert out["gamma"] == 0.8
    assert out["q_max_usdc"] == 250.0
    assert out["base_size_usdc"] == 50.0
    assert "q_max_usdc" in FORBIDDEN_KEYS


def test_requires_paper_for_unsafe_even_if_llm_says_no() -> None:
    assert requires_paper({"gamma": 0.7}, llm_flag=False) is False
    assert "gamma" in SAFE_IMMEDIATE_KEYS
    # Unknown / non-allowlist key forces paper.
    assert requires_paper({"use_as_reservation_price": True}, llm_flag=False) is True
    assert requires_paper({"gamma": 0.7}, llm_flag=True) is True


def test_immediate_apply_safe_override(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "h.db")

    def llm(**_kwargs):
        return {
            "diagnosis": "too tight",
            "suggestion": "widen c_vol",
            "expected_impact_pct": 2.0,
            "paper_validation_required": False,
            "profile_overrides": {"c_vol": 2.0},
        }

    improver = SelfImprover(history=hist, llm=llm)
    improver.set_live_profile({"c_vol": 1.2, "gamma": 0.5})
    result = improver.run(_low_hit_rate_eval())
    assert result.triggered
    assert result.applied
    assert result.promoted
    assert result.paper_validated is False
    assert improver.live_profile["c_vol"] == 2.0
    assert hist.latest() is not None
    assert hist.latest().source == "self_improve"
    hist.close()


def test_draft_paper_promote_flow(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "h.db")
    calls: list[dict] = []

    def llm(**_kwargs):
        return {
            "diagnosis": "join touch experiment",
            "suggestion": "enable join_best_bid",
            "expected_impact_pct": 1.0,
            "paper_validation_required": True,
            "profile_overrides": {"join_best_bid": True, "min_edge_ticks": 0},
        }

    def validator(draft: dict) -> bool:
        calls.append(draft)
        return True

    improver = SelfImprover(
        history=hist, llm=llm, paper_validator=validator, paper_duration_s=1.0
    )
    improver.set_live_profile(
        {"join_best_bid": False, "min_edge_ticks": 1, "gamma": 0.5}
    )
    # join_best_bid is safe-list but LLM required paper → paper path.
    result = improver.run(_decaying_eval())
    assert result.triggered
    assert result.promoted
    assert result.paper_validated
    assert result.applied
    assert len(calls) == 1
    assert improver.live_profile["join_best_bid"] is True
    assert improver.live_profile["min_edge_ticks"] == 0
    row = hist.latest()
    assert row is not None
    assert row.paper_validated is True
    assert "paper-ok" in row.reason
    hist.close()


def test_draft_paper_reject_flow(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "h.db")

    def llm(**_kwargs):
        return {
            "diagnosis": "deeper micro",
            "suggestion": "raise micro_levels",
            "expected_impact_pct": 5.0,
            "paper_validation_required": True,
            # micro_levels is neither forbidden nor immediate-safe → paper path
            "profile_overrides": {"micro_levels": 5},
        }

    improver = SelfImprover(
        history=hist,
        llm=llm,
        paper_validator=lambda _d: False,
    )
    live = {"micro_levels": 3, "gamma": 0.5}
    improver.set_live_profile(live)
    result = improver.run(_decaying_eval())
    assert result.rejected
    assert not result.promoted
    assert not result.applied
    assert improver.live_profile["micro_levels"] == 3
    assert improver.draft_profile is not None
    assert improver.draft_profile["micro_levels"] == 5
    hist.close()


def test_forbidden_only_yields_no_safe_overrides(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "h.db")

    def llm(**_kwargs):
        return {
            "diagnosis": "try AS",
            "suggestion": "turn on advanced quoting",
            "expected_impact_pct": 5.0,
            "paper_validation_required": True,
            "profile_overrides": {"use_as_reservation_price": True},
        }

    improver = SelfImprover(history=hist, llm=llm)
    improver.set_live_profile({"use_as_reservation_price": False, "gamma": 0.5})
    result = improver.run(_decaying_eval())
    assert not result.promoted
    assert "no_safe_overrides" in result.errors
    assert "use_as_reservation_price" in (result.suggestion.stripped_keys if result.suggestion else [])
    hist.close()


def test_rollback_after_promote(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "h.db")

    def llm(**_kwargs):
        return {
            "diagnosis": "x",
            "suggestion": "raise trend_vol_ratio",
            "expected_impact_pct": 0.5,
            "paper_validation_required": False,
            "profile_overrides": {"trend_vol_ratio": 4.0},
        }

    improver = SelfImprover(history=hist, llm=llm)
    improver.set_live_profile({"trend_vol_ratio": 2.0})
    before_ts = 1_700_000_000.0
    # Seed a prior row so rollback has a target.
    hist.append(
        old_profile={"trend_vol_ratio": 2.0},
        new_profile={"trend_vol_ratio": 2.0},
        source="manual",
        reason="baseline",
        ts=before_ts,
    )
    result = improver.run(_low_hit_rate_eval())
    assert result.promoted
    assert improver.live_profile["trend_vol_ratio"] == 4.0
    restored = hist.rollback(before_ts)
    assert restored["trend_vol_ratio"] == 2.0
    hist.close()


def test_reasoning_model_constant() -> None:
    assert REASONING_MODEL == "grok-4-1-fast-reasoning"


def test_no_trigger_when_healthy(tmp_path: Path) -> None:
    hist = ProfileHistory(tmp_path / "h.db")
    called = {"n": 0}

    def llm(**_kwargs):
        called["n"] += 1
        return {}

    improver = SelfImprover(history=hist, llm=llm)
    improver.set_live_profile({"gamma": 0.5})
    result = improver.run(SelfEvaluation())
    assert not result.triggered
    assert called["n"] == 0
    hist.close()


def test_parse_llm_json_fence_and_preamble() -> None:
    from polymaker.intelligence.self_improve import parse_llm_json
    raw = "Here you go:\n```json\n{\"diagnosis\": \"x\", \"suggestion\": \"y\"}\n```\n"
    data = parse_llm_json(raw)
    assert data["diagnosis"] == "x"


def test_coerce_and_strip_advanced() -> None:
    from polymaker.intelligence.self_improve import coerce_overrides, strip_forbidden
    live = {"gamma": 0.5, "layers": 3, "join_best_bid": False}
    coerced = coerce_overrides(live, {"gamma": "0.8", "layers": "2", "join_best_bid": "true"})
    assert coerced["gamma"] == 0.8
    assert coerced["layers"] == 2
    assert coerced["join_best_bid"] is True
    clean, stripped = strip_forbidden({"gamma": 1.0, "use_as_reservation_price": True, "q_max_usdc": 9})
    assert "gamma" in clean
    assert "use_as_reservation_price" in stripped
    assert "q_max_usdc" in stripped


def test_dry_run_does_not_promote(tmp_path: Path) -> None:
    from polymaker.intelligence.profile_history import ProfileHistory
    from polymaker.intelligence.self_improve import SelfImprover

    hist = ProfileHistory(tmp_path / "h.db")

    def llm(**_k):
        return {
            "diagnosis": "d", "suggestion": "widen", "expected_impact_pct": 1.0,
            "paper_validation_required": False, "profile_overrides": {"c_vol": 2.5},
        }

    improver = SelfImprover(history=hist, llm=llm)
    improver.set_live_profile({"c_vol": 1.2})
    result = improver.run(_low_hit_rate_eval(), dry_run=True)
    assert result.dry_run
    assert not result.promoted
    assert improver.live_profile["c_vol"] == 1.2
    assert result.diff["c_vol"] == (1.2, 2.5)
    hist.close()


def test_draft_store_roundtrip(tmp_path: Path) -> None:
    from polymaker.intelligence.self_improve import DraftStore
    store = DraftStore(tmp_path / "drafts")
    store.save("live_scaled", {"gamma": 0.9}, meta={"reason": "test"})
    loaded = store.load("live_scaled")
    assert loaded is not None
    assert loaded["draft"]["gamma"] == 0.9
    store.clear("live_scaled")
    assert store.load("live_scaled") is None
