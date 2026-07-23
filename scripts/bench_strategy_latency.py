#!/usr/bin/env python3
"""Benchmark strategy latency: measure per-recompute time and per-quote time.

Usage:
  uv run python scripts/bench_strategy_latency.py --n-iterations 1000
  uv run python scripts/bench_strategy_latency.py --compare-models
"""

from __future__ import annotations

import argparse
import statistics
import time

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, TokenMeta
from polymaker.marketdata.orderbook import BookView, OrderBook
from polymaker.strategy.advanced_quoting import (
    AdvancedQuoteInputs,
    compute_advanced_quotes,
)
from polymaker.strategy.avellaneda_stoikov import ASInputs, avellaneda_stoikov
from polymaker.strategy.kelly import KellyInputs, kelly_size
from polymaker.strategy.quoting import (
    QuoteInputs,
    compute_fair_value,
    construct_quotes,
)


def _make_meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xtest",
        question="Test?",
        slug="test",
        tokens=(TokenMeta("yes-tok", "Yes"), TokenMeta("no-tok", "No")),
        tick_size=0.001,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=5.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
        liquidity_num=10000.0,
    )


def _make_book(mid: float = 0.50) -> OrderBook:
    book = OrderBook(tick_size=0.001)
    bids = [(mid - 0.002, 100.0), (mid - 0.003, 200.0), (mid - 0.004, 300.0)]
    asks = [(mid + 0.002, 100.0), (mid + 0.003, 200.0), (mid + 0.004, 300.0)]
    book.apply_snapshot(bids, asks, time.time(), "hash")
    return book


def bench_simple_quote(n: int) -> dict[str, float]:
    """Benchmark the simple construct_quotes model."""
    meta = _make_meta()
    profile = StrategyProfile()
    yes_book = _make_book()
    no_book = _make_book()
    yes_view = yes_book.view()
    no_view = no_book.view()
    pos_yes = Position("yes-tok")
    pos_no = Position("no-tok")

    times = []
    for _ in range(n):
        start = time.perf_counter()
        fv = compute_fair_value(0.50, 0.0, meta.tick_size)
        inp = QuoteInputs(
            meta=meta, regime=None, fv=fv, vol_short=0.01,
            toxicity=0.0, yes_view=yes_view, no_view=no_view,
            pos_yes=pos_yes, pos_no=pos_no, profile=profile, now=time.time(),
        )
        construct_quotes(inp)
        times.append((time.perf_counter() - start) * 1e6)  # microseconds

    return {
        "mean_us": statistics.mean(times),
        "median_us": statistics.median(times),
        "p95_us": sorted(times)[int(len(times) * 0.95)],
        "p99_us": sorted(times)[int(len(times) * 0.99)],
        "min_us": min(times),
        "max_us": max(times),
    }


def bench_advanced_quote(n: int) -> dict[str, float]:
    """Benchmark the advanced (Avellaneda-Stoikov + Kelly) model."""
    meta = _make_meta()
    profile = StrategyProfile()
    yes_view = BookView(0.498, 100.0, 0.502, 100.0, 0.497, 0.503, 100.0, 100.0)
    no_view = BookView(0.498, 100.0, 0.502, 100.0, 0.497, 0.503, 100.0, 100.0)
    pos_yes = Position("yes-tok")
    pos_no = Position("no-tok")

    times = []
    for _ in range(n):
        start = time.perf_counter()
        inp = AdvancedQuoteInputs(
            meta=meta, fv=0.50, sigma=0.01,
            yes_view=yes_view, no_view=no_view,
            pos_yes=pos_yes, pos_no=pos_no, profile=profile,
            bankroll_usdc=1000.0, now=time.time(),
        )
        compute_advanced_quotes(inp)
        times.append((time.perf_counter() - start) * 1e6)

    return {
        "mean_us": statistics.mean(times),
        "median_us": statistics.median(times),
        "p95_us": sorted(times)[int(len(times) * 0.95)],
        "p99_us": sorted(times)[int(len(times) * 0.99)],
        "min_us": min(times),
        "max_us": max(times),
    }


def bench_avellaneda_stoikov(n: int) -> dict[str, float]:
    """Benchmark just the Avellaneda-Stoikov model."""
    times = []
    for _ in range(n):
        start = time.perf_counter()
        avellaneda_stoikov(ASInputs(
            mid=0.50, inventory=10.0, sigma=0.01,
            time_horizon_s=3600.0, gamma=0.1, kappa=10.0,
        ))
        times.append((time.perf_counter() - start) * 1e6)

    return {
        "mean_us": statistics.mean(times),
        "median_us": statistics.median(times),
        "p95_us": sorted(times)[int(len(times) * 0.95)],
        "p99_us": sorted(times)[int(len(times) * 0.99)],
        "min_us": min(times),
        "max_us": max(times),
    }


def bench_kelly(n: int) -> dict[str, float]:
    """Benchmark just the Kelly sizing model."""
    times = []
    for _ in range(n):
        start = time.perf_counter()
        kelly_size(KellyInputs(
            edge=0.01, sigma=0.01, time_horizon_s=3600.0,
            bankroll_usdc=1000.0, inventory_shares=0.0,
            max_inventory_shares=500.0, price=0.50,
        ))
        times.append((time.perf_counter() - start) * 1e6)

    return {
        "mean_us": statistics.mean(times),
        "median_us": statistics.median(times),
        "p95_us": sorted(times)[int(len(times) * 0.95)],
        "p99_us": sorted(times)[int(len(times) * 0.99)],
        "min_us": min(times),
        "max_us": max(times),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n-iterations", type=int, default=10000, help="iterations per benchmark")
    ap.add_argument("--compare-models", action="store_true", help="compare simple vs advanced")
    args = ap.parse_args()

    n = args.n_iterations
    print(f"Running {n} iterations per benchmark...\n")

    if args.compare_models:
        print("=== Simple Quote (construct_quotes) ===")
        simple = bench_simple_quote(n)
        for k, v in simple.items():
            print(f"  {k}: {v:.3f}")

        print("\n=== Advanced Quote (Avellaneda-Stoikov + Kelly) ===")
        advanced = bench_advanced_quote(n)
        for k, v in advanced.items():
            print(f"  {k}: {v:.3f}")

        print("\n=== Speedup ===")
        if simple["mean_us"] > 0:
            speedup = simple["mean_us"] / advanced["mean_us"]
            print(f"  mean: {speedup:.2f}x")
        if simple["p99_us"] > 0:
            speedup_p99 = simple["p99_us"] / advanced["p99_us"]
            print(f"  p99: {speedup_p99:.2f}x")
    else:
        print("=== Avellaneda-Stoikov ===")
        as_res = bench_avellaneda_stoikov(n)
        for k, v in as_res.items():
            print(f"  {k}: {v:.3f}")

        print("\n=== Kelly Sizing ===")
        kelly_res = bench_kelly(n)
        for k, v in kelly_res.items():
            print(f"  {k}: {v:.3f}")

        print("\n=== Advanced Quote (combined) ===")
        adv_res = bench_advanced_quote(n)
        for k, v in adv_res.items():
            print(f"  {k}: {v:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
