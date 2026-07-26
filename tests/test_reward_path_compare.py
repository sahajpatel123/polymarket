"""Tests for reward_path_compare denominator-artifact flag logic."""

from __future__ import annotations


def test_denominator_artifact_flag_logic():
    b = {"reward_accrual_usdc": 7.66, "ev_per_quote_usdc": 0.0015, "n_quote": 244, "n_fill": 0}
    c = {"reward_accrual_usdc": 7.66, "ev_per_quote_usdc": 0.0019, "n_quote": 195, "n_fill": 0}
    artifact = (
        c["n_fill"] == 0
        and abs(c["reward_accrual_usdc"] - b["reward_accrual_usdc"]) < 1e-9
        and c["n_quote"] < b["n_quote"]
        and c["ev_per_quote_usdc"] > b["ev_per_quote_usdc"]
    )
    assert artifact is True
