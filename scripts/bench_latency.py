#!/usr/bin/env python3
"""Latency/throughput benchmark for the polymaker hot path.

Measures, on a fixed replayed data window, the time from "market-data received"
to "quote/order submitted" — p50/p95/p99 — and events-processed-per-second.

The hot path measured is:
  apply_journal_event (book update applied) -> _recompute (strategy + reconcile)

This is the pure-computation path with no I/O. The gateway/network layer is
benchmarked separately (see P1-04).

Usage:
  uv run python scripts/bench_latency.py [--events N] [--seed S] [--profile NAME]
  uv run python scripts/bench_latency.py --events 5000 --seed 42

Output: JSON to stdout with latency percentiles and throughput.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics import MetricsLogger
from polymaker.replay import ReplayState, apply_journal_event, load_journal
from polymaker.replay import _recompute as replay_recompute
from polymaker.strategy.quoting import QuoteInputs, compute_fair_value, construct_quotes
from polymaker.strategy.regime import RegimeInputs


YES_TOKEN = "0x" + "a" * 64
NO_TOKEN = "0x" + "b" * 64
CONDITION_ID = "0x" + "c" * 64


def _default_meta() -> MarketMeta:
    return MarketMeta(
        condition_id=CONDITION_ID,
        question="Benchmark fixture market",
        slug="bench-fixture",
        tokens=(TokenMeta(YES_TOKEN, "Yes"), TokenMeta(NO_TOKEN, "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def generate_synthetic_journal(
    path: Path,
    n_events: int,
    seed: int,
    tick_size: float = 0.01,
) -> None:
    """Generate a realistic synthetic journal JSONL for benchmarking.

    Produces a mix of book snapshots, price changes, and trade prints that
    exercise the full strategy pipeline (quoting, regime, reconcile).
    """
    rng = random.Random(seed)
    t0 = 1_700_000_000.0
    rows: list[dict] = []

    # Initial book snapshot for both tokens
    for tok in (YES_TOKEN, NO_TOKEN):
        rows.append({
            "ts": t0,
            "kind": "book",
            "data": {
                "market": CONDITION_ID,
                "asset_id": tok,
                "bids": [{"price": "0.48", "size": "500"}, {"price": "0.47", "size": "500"}],
                "asks": [{"price": "0.52", "size": "500"}, {"price": "0.53", "size": "500"}],
                "timestamp": str(int(t0 * 1000)),
                "tick_size": str(tick_size),
            },
        })

    mid = 0.50
    ts = t0 + 1.0
    for i in range(n_events):
        ts += rng.uniform(0.05, 0.5)  # irregular arrival
        r = rng.random()

        if r < 0.4:
            # price change on YES
            price = round(mid + rng.uniform(-0.03, 0.03), 2)
            size = round(rng.uniform(50, 500), 1)
            side = "BUY" if rng.random() < 0.5 else "SELL"
            rows.append({
                "ts": ts,
                "kind": "price_change",
                "data": {
                    "market": CONDITION_ID,
                    "timestamp": str(int(ts * 1000)),
                    "price_changes": [
                        {"asset_id": YES_TOKEN, "price": str(price), "size": str(size), "side": side},
                    ],
                },
            })
        elif r < 0.6:
            # trade print on YES
            price = round(mid + rng.uniform(-0.02, 0.02), 2)
            size = round(rng.uniform(10, 200), 1)
            side = "BUY" if rng.random() < 0.5 else "SELL"
            rows.append({
                "ts": ts,
                "kind": "last_trade_price",
                "data": {
                    "market": CONDITION_ID,
                    "asset_id": YES_TOKEN,
                    "price": str(price),
                    "size": str(size),
                    "side": side,
                    "timestamp": str(int(ts * 1000)),
                },
            })
        elif r < 0.8:
            # periodic book snapshot (less frequent)
            mid = round(mid + rng.uniform(-0.01, 0.01), 2)
            mid = max(0.10, min(0.90, mid))
            rows.append({
                "ts": ts,
                "kind": "book",
                "data": {
                    "market": CONDITION_ID,
                    "asset_id": YES_TOKEN,
                    "bids": [
                        {"price": str(round(mid - 0.02, 2)), "size": str(round(rng.uniform(300, 800), 1))},
                        {"price": str(round(mid - 0.03, 2)), "size": str(round(rng.uniform(300, 800), 1))},
                    ],
                    "asks": [
                        {"price": str(round(mid + 0.02, 2)), "size": str(round(rng.uniform(300, 800), 1))},
                        {"price": str(round(mid + 0.03, 2)), "size": str(round(rng.uniform(300, 800), 1))},
                    ],
                    "timestamp": str(int(ts * 1000)),
                    "tick_size": str(tick_size),
                },
            })
        else:
            # price change on NO
            price = round(0.50 + rng.uniform(-0.03, 0.03), 2)
            size = round(rng.uniform(50, 500), 1)
            side = "BUY" if rng.random() < 0.5 else "SELL"
            rows.append({
                "ts": ts,
                "kind": "price_change",
                "data": {
                    "market": CONDITION_ID,
                    "timestamp": str(int(ts * 1000)),
                    "price_changes": [
                        {"asset_id": NO_TOKEN, "price": str(price), "size": str(size), "side": side},
                    ],
                },
            })

    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def bench_replay(journal_path: Path, meta: MarketMeta, profile: StrategyProfile,
                 metrics_path: Path) -> dict:
    """Benchmark the replay hot path: apply_journal_event -> _recompute.

    Returns latency percentiles and throughput.
    """
    rows = load_journal(journal_path)
    if not rows:
        return {"error": "no events in journal"}

    st = ReplayState(meta=meta, profile=profile)
    st.metrics = MetricsLogger(metrics_path, enabled=True)
    st.metrics.emit(
        "market_meta",
        condition_id=meta.condition_id,
        slug=meta.slug,
        tick_size=meta.tick_size,
        rewards_daily_rate=meta.rewards_daily_rate,
        rewards_max_spread=meta.rewards_max_spread,
        rewards_min_size=meta.rewards_min_size,
        rebate_rate=meta.rebate_rate,
    )

    latencies_us: list[float] = []
    recompute_count = 0
    total_events = len(rows)

    # Warm up: process first 10 events without measuring
    warmup = min(10, total_events)
    for row in rows[:warmup]:
        if apply_journal_event(st, row):
            replay_recompute(st, float(row.get("ts") or 0.0))

    # Measured run
    t_start = time.perf_counter()
    for row in rows[warmup:]:
        ts = float(row.get("ts") or 0.0)
        t_apply = time.perf_counter()
        dirty = apply_journal_event(st, row)
        if dirty:
            replay_recompute(st, ts)
            t_recompute = time.perf_counter()
            latencies_us.append((t_recompute - t_apply) * 1_000_000)
            recompute_count += 1
    t_end = time.perf_counter()

    st.metrics.close()

    wall_s = t_end - t_start
    events_per_s = (total_events - warmup) / wall_s if wall_s > 0 else 0.0

    if latencies_us:
        latencies_us.sort()
        n = len(latencies_us)
        p50 = latencies_us[int(n * 0.50)]
        p95 = latencies_us[int(n * 0.95)]
        p99 = latencies_us[int(n * 0.99)] if n > 1 else latencies_us[-1]
        mean = statistics.mean(latencies_us)
        stdev = statistics.stdev(latencies_us) if n > 1 else 0.0
    else:
        p50 = p95 = p99 = mean = stdev = 0.0

    return {
        "events_total": total_events,
        "events_warmup": warmup,
        "recomputes": recompute_count,
        "wall_time_s": round(wall_s, 6),
        "events_per_s": round(events_per_s, 1),
        "latency_us": {
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
            "mean": round(mean, 2),
            "stdev": round(stdev, 2),
        },
        "metrics_path": str(metrics_path),
    }


def bench_pure_strategy(meta: MarketMeta, profile: StrategyProfile,
                        n_iterations: int = 10000) -> dict:
    """Benchmark the pure strategy functions in isolation (no book parsing).

    Measures construct_quotes + reconcile on a pre-built book view.
    This isolates the strategy math from the market-data parsing overhead.
    """
    from polymaker.domain import Position, Regime, Side
    from polymaker.execution.reconciler import reconcile
    from polymaker.marketdata.orderbook import BookView, OrderBook
    from polymaker.strategy.estimators import FlowEstimator, VolEstimator, MarkoutTracker, MarketEstimators

    # Build a representative book
    book = OrderBook(tick_size=meta.tick_size)
    book.apply_snapshot(
        bids=[(0.48, 500), (0.47, 500), (0.46, 500)],
        asks=[(0.52, 500), (0.53, 500), (0.54, 500)],
        ts=1_700_000_000.0,
    )
    yes_view = book.view()
    no_view = book.view()

    # Build estimators
    est = MarketEstimators(
        vol=VolEstimator(profile.vol_short_halflife_s, profile.vol_long_halflife_s),
        flow=FlowEstimator(profile.flow_ewma_halflife_s),
        markout=MarkoutTracker(),
    )
    # Seed estimators with some data
    for i in range(20):
        est.vol.update(0.50 + (i % 5) * 0.001, 1_700_000_000.0 + i)
        est.flow.update(Side.BUY, 100.0, 1_700_000_000.0 + i)
        est.markout.evaluate(0.50, 1_700_000_000.0 + i)

    micro = book.microprice(profile.micro_levels)
    fv = compute_fair_value(micro, est.flow.z, meta.tick_size)
    est.on_fair_value(fv, 1_700_000_000.0)

    pos_yes = Position(YES_TOKEN, 100, 0.50)
    pos_no = Position(NO_TOKEN, 50, 0.50)
    live = []

    latencies_us: list[float] = []

    # Warm up
    for _ in range(100):
        tq = construct_quotes(QuoteInputs(
            meta=meta, regime=Regime.QUIET, fv=fv, vol_short=est.vol.short,
            toxicity=est.markout.toxicity, yes_view=yes_view, no_view=no_view,
            pos_yes=pos_yes, pos_no=pos_no, profile=profile, now=1_700_000_000.0,
        ))
        plan = reconcile(tq, live, tick=meta.tick_size,
                         reprice_ticks=profile.reprice_ticks, resize_frac=profile.resize_frac)

    # Measured
    t_start = time.perf_counter()
    for _ in range(n_iterations):
        t_iter = time.perf_counter()
        tq = construct_quotes(QuoteInputs(
            meta=meta, regime=Regime.QUIET, fv=fv, vol_short=est.vol.short,
            toxicity=est.markout.toxicity, yes_view=yes_view, no_view=no_view,
            pos_yes=pos_yes, pos_no=pos_no, profile=profile, now=1_700_000_000.0,
        ))
        plan = reconcile(tq, live, tick=meta.tick_size,
                         reprice_ticks=profile.reprice_ticks, resize_frac=profile.resize_frac)
        t_end = time.perf_counter()
        latencies_us.append((t_end - t_iter) * 1_000_000)
    t_wall = time.perf_counter() - t_start

    latencies_us.sort()
    n = len(latencies_us)
    p50 = latencies_us[int(n * 0.50)]
    p95 = latencies_us[int(n * 0.95)]
    p99 = latencies_us[int(n * 0.99)] if n > 1 else latencies_us[-1]
    mean = statistics.mean(latencies_us)

    ops_per_s = n_iterations / t_wall if t_wall > 0 else 0.0

    return {
        "iterations": n_iterations,
        "wall_time_s": round(t_wall, 6),
        "ops_per_s": round(ops_per_s, 1),
        "latency_us": {
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
            "mean": round(mean, 2),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", type=int, default=5000,
                    help="Number of synthetic journal events to generate (default: 5000)")
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for synthetic data")
    ap.add_argument("--profile", default="newsom-mm", help="Strategy profile name")
    ap.add_argument("--journal", default=None, help="Use existing journal instead of generating")
    ap.add_argument("--pure-only", action="store_true", help="Only run the pure strategy benchmark")
    args = ap.parse_args()

    meta = _default_meta()
    profile = StrategyProfile()

    results: dict = {"seed": args.seed, "events": args.events}

    if not args.pure_only:
        if args.journal:
            journal_path = Path(args.journal)
        else:
            journal_path = Path(f"perf/bench_journal_{args.seed}_{args.events}.jsonl")
            if not journal_path.exists():
                print(f"Generating synthetic journal: {journal_path}", file=sys.stderr)
                generate_synthetic_journal(journal_path, args.events, args.seed)

        metrics_path = Path(f"perf/bench_metrics_{args.seed}_{args.events}.jsonl")
        print(f"Running replay benchmark on {journal_path}...", file=sys.stderr)
        replay_result = bench_replay(journal_path, meta, profile, metrics_path)
        results["replay"] = replay_result
        print(f"  events={replay_result['events_total']} "
              f"recomputes={replay_result['recomputes']} "
              f"p50={replay_result['latency_us']['p50']:.1f}us "
              f"p95={replay_result['latency_us']['p95']:.1f}us "
              f"p99={replay_result['latency_us']['p99']:.1f}us "
              f"eps={replay_result['events_per_s']:.0f}", file=sys.stderr)

    print("Running pure strategy benchmark...", file=sys.stderr)
    pure_result = bench_pure_strategy(meta, profile, n_iterations=10000)
    results["pure_strategy"] = pure_result
    print(f"  iters={pure_result['iterations']} "
          f"p50={pure_result['latency_us']['p50']:.1f}us "
          f"p95={pure_result['latency_us']['p95']:.1f}us "
          f"p99={pure_result['latency_us']['p99']:.1f}us "
          f"ops/s={pure_result['ops_per_s']:.0f}", file=sys.stderr)

    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
