"""Benchmark validity gates — zero-activity must not PASS."""

from __future__ import annotations

from polymaker.benchmark import (
    BenchmarkStatus,
    ValidityConfig,
    check_capital_feasibility,
    evaluate_benchmark,
)


def test_zero_quotes_insufficient_data() -> None:
    r = evaluate_benchmark(
        n_quote=0, n_fill=0, n_mark=100, n_markets=1,
        runtime_s=3600, n_trade_prints=1000,
        cfg=ValidityConfig(min_quotes=50, min_fills=10, min_marks=20),
    )
    assert r.status is BenchmarkStatus.INSUFFICIENT_DATA
    assert not r.ok


def test_quotes_no_fills_insufficient() -> None:
    r = evaluate_benchmark(
        n_quote=500, n_fill=0, n_mark=100, n_markets=1,
        runtime_s=3600, n_trade_prints=1000,
        cfg=ValidityConfig(min_quotes=50, min_fills=10),
    )
    assert r.status is BenchmarkStatus.INSUFFICIENT_DATA


def test_divergence_is_fail() -> None:
    r = evaluate_benchmark(
        n_quote=500, n_fill=100, n_mark=100, n_markets=1,
        runtime_s=3600, n_trade_prints=1000,
        state_divergence_events=1,
    )
    assert r.status is BenchmarkStatus.FAIL


def test_fills_after_cancel_is_fail() -> None:
    r = evaluate_benchmark(
        n_quote=500, n_fill=100, n_mark=100, n_markets=1,
        runtime_s=3600, n_trade_prints=1000,
        fills_after_cancel=2,
    )
    assert r.status is BenchmarkStatus.FAIL


def test_healthy_run_passes() -> None:
    r = evaluate_benchmark(
        n_quote=500, n_fill=100, n_mark=200, n_markets=3,
        runtime_s=3600, n_trade_prints=500,
        cfg=ValidityConfig(
            min_quotes=50, min_fills=10, min_marks=20,
            min_runtime_s=60, min_trade_prints=20,
        ),
    )
    assert r.status is BenchmarkStatus.PASS
    assert r.ok


def test_capital_insufficient_for_tiny_bankroll() -> None:
    c = check_capital_feasibility(
        bankroll=1.5,
        exchange_min_shares=5.0,
        reward_min_shares=10.0,
        typical_price=0.5,
        layers=2,
    )
    assert not c.ok
    assert "INSUFFICIENT" in c.reason


def test_capital_ok_for_reasonable_bankroll() -> None:
    c = check_capital_feasibility(
        bankroll=100.0,
        exchange_min_shares=5.0,
        reward_min_shares=10.0,
        typical_price=0.5,
        layers=2,
    )
    assert c.ok


def test_insufficient_capital_status() -> None:
    r = evaluate_benchmark(
        n_quote=0, n_fill=0, n_mark=0, capital_ok=False,
    )
    assert r.status is BenchmarkStatus.INSUFFICIENT_CAPITAL


def test_financial_claim_rejects_optimistic_and_monopoly() -> None:
    from polymaker.benchmark import evaluate_financial_claim

    v = evaluate_benchmark(
        n_quote=500, n_fill=100, n_mark=200, n_markets=1,
        runtime_s=3600, n_trade_prints=500,
        cfg=ValidityConfig(min_quotes=50, min_fills=10, min_marks=20),
    )
    assert v.status is BenchmarkStatus.PASS
    fin_opt = evaluate_financial_claim(
        validity=v,
        honest_pnl={"financial_claim_ok": True, "claim_blockers": []},
        fill_mode="optimistic",
    )
    assert fin_opt.status is BenchmarkStatus.INSUFFICIENT_DATA
    fin_mono = evaluate_financial_claim(
        validity=v,
        honest_pnl={
            "financial_claim_ok": False,
            "claim_blockers": ["monopoly_rewards_only_positive"],
            "pnl_monopoly_diagnostic_usdc": 80.0,
            "pnl_without_rewards_usdc": -1.0,
        },
        fill_mode="conservative",
    )
    assert fin_mono.status is BenchmarkStatus.INSUFFICIENT_DATA
