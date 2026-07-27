"""Prompt templates are importable and non-empty."""

from __future__ import annotations

from polymaker.intelligence import prompts
from polymaker.intelligence.prompts import (
    PROMPT_VERSION,
    PROMPTS,
    TOOL_SCHEMAS,
    prompt_eod_review,
    prompt_oversight_commentary,
    prompt_rank_markets,
    prompt_regime_comment,
    prompt_self_improve,
)


def test_prompt_version_and_registry() -> None:
    assert PROMPT_VERSION
    assert set(PROMPTS) == {
        "rank_markets",
        "regime_comment",
        "eod_review",
        "self_improve",
        "oversight_commentary",
    }
    assert "rank_markets" in TOOL_SCHEMAS
    assert "oversight_report" in TOOL_SCHEMAS


def test_each_prompt_non_empty() -> None:
    a_sys, a_user = prompt_rank_markets([{"condition_id": "x", "rewards_daily_rate": 1}])
    assert a_sys.strip() and a_user.strip()
    assert "rank_markets" in a_user

    b_sys, b_user = prompt_regime_comment(
        market="m", math_says="TRENDING", features={"flow_z": 2.0}
    )
    assert b_sys.strip() and b_user.strip()

    c_sys, c_user = prompt_eod_review({"pnl": 1.0})
    assert c_sys.strip() and c_user.strip()

    d_sys, d_user = prompt_self_improve({"decay": True})
    assert d_sys.strip() and d_user.strip()

    e_sys, e_user = prompt_oversight_commentary({"pnl": 0, "anomalies": []})
    assert e_sys.strip() and e_user.strip()
    assert "oversight_report" in e_user


def test_module_docstring() -> None:
    assert prompts.__doc__ and "Versioned" in prompts.__doc__
