#!/usr/bin/env python3
"""Stress test: replay the recorded journal N times to simulate 12+ hours.

Since Polymarket is down, we can't connect to a live feed. But we can
stress-test the code path by replaying the same 8.37h journal multiple
times back-to-back. This is a genuine stress test of:
- The replay engine's robustness to repeated runs
- Memory stability (no leaks over N runs)
- The metrics aggregation across cycles
- The risk/quote accounting consistency

Usage:
  uv run python scripts/stress_12h.py --n-cycles 20 --profile live_scaled --bankroll 100
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from polymaker.config import Config, StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay import run_replay


def _make_meta_from_metrics(metrics_path: Path, condition_id: str) -> MarketMeta | None:
    """Reconstruct a minimal MarketMeta from the metrics log market_meta entry."""
    if not metrics_path.exists():
        return None
    for line in metrics_path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("event") != "market_meta":
            continue
        if obj.get("condition_id") != condition_id:
            continue
        # Find tokens in the quote events
        yes_token = no_token = ""
        for ln in metrics_path.read_text().splitlines():
            try:
                q = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if q.get("event") == "quote" and q.get("condition_id") == condition_id:
                tok = str(q.get("token_id") or "")
                if not tok:
                    continue
                if not yes_token:
                    yes_token = tok
                elif not no_token and tok != yes_token:
                    no_token = tok
                    break
        if not yes_token or not no_token:
            return None
        return MarketMeta(
            condition_id=condition_id,
            question=f"backtest-{condition_id[:8]}",
            slug=f"backtest-{condition_id[:8]}",
            tokens=(TokenMeta(yes_token, "Yes"), TokenMeta(no_token, "No")),
            tick_size=0.001,
            neg_risk=False,
            min_order_size=5.0,
            rewards_min_size=float(obj.get("rewards_min_size") or 5.0),
            rewards_max_spread=float(obj.get("rewards_max_spread") or 3.0),
            rewards_daily_rate=float(obj.get("rewards_daily_rate") or 0.0),
            maker_fee_bps=0,
            taker_fee_bps=int(obj.get("taker_fee_bps") or 400),
            fees_enabled=bool(obj.get("fees_enabled") or True),
            end_date_iso=None,
            event_id=None,
            rebate_rate=float(obj.get("rebate_rate") or 0.25),
        )
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True, help="path to journal JSONL")
    ap.add_argument("--metrics-dir", required=True, help="dir with existing metrics files for Meta reconstruction")
    ap.add_argument("--n-cycles", type=int, default=20, help="replay the journal N times back-to-back")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--out-dir", default="/tmp/stress_12h")
    args = ap.parse_args()

    journal_path = Path(args.journal)
    metrics_dir = Path(args.metrics_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not journal_path.exists():
        print(f"ERROR: journal not found: {journal_path}", file=sys.stderr)
        return 1

    cfg = Config.load(args.config_dir, load_env=False)
    if args.profile not in cfg.profiles:
        print(f"ERROR: profile {args.profile!r} not in config", file=sys.stderr)
        return 1
    profile = cfg.profiles[args.profile]

    # Discover condition IDs from metrics dir
    cids: list[str] = []
    for mp in sorted(metrics_dir.glob("metrics_*.jsonl")):
        try:
            for line in mp.read_text().splitlines()[:5]:
                obj = json.loads(line)
                if obj.get("event") == "market_meta":
                    cid = str(obj.get("condition_id"))
                    if cid not in cids:
                        cids.append(cid)
                    break
        except (json.JSONDecodeError, OSError):
            continue

    if not cids:
        print(f"ERROR: no condition IDs found in {metrics_dir}", file=sys.stderr)
        return 1

    print(f"=== 12h Stress Test: replaying {journal_path} {args.n_cycles}x ===")
    print(f"Profile: {args.profile}")
    print(f"Markets: {cids}")
    print()

    cycle_results: list[dict] = []
    total_start = time.time()

    for cycle in range(args.n_cycles):
        cycle_start = time.time()
        cycle_data: dict = {"cycle": cycle, "fills": 0, "quotes": 0, "cancels": 0, "elapsed_s": 0.0}

        for cid in cids:
            metrics_src = metrics_dir / f"metrics_{cid[:12]}.jsonl"
            meta = _make_meta_from_metrics(metrics_src, cid)
            if meta is None:
                continue
            metrics_out = out_dir / f"cycle_{cycle:02d}_metrics_{cid[:12]}.jsonl"
            result = run_replay(journal_path, meta, profile, metrics_out)
            cycle_data["fills"] += result.n_fill
            cycle_data["quotes"] += result.n_quote
            cycle_data["cancels"] += result.n_cancel

        cycle_data["elapsed_s"] = time.time() - cycle_start
        cycle_results.append(cycle_data)
        print(
            f"  cycle {cycle + 1:02d}/{args.n_cycles}: "
            f"quotes={cycle_data['quotes']:>3} cancels={cycle_data['cancels']:>3} "
            f"fills={cycle_data['fills']:>3} elapsed={cycle_data['elapsed_s']:.2f}s"
        )

    total_elapsed = time.time() - total_start

    # Aggregate
    total_quotes = sum(c["quotes"] for c in cycle_results)
    total_cancels = sum(c["cancels"] for c in cycle_results)
    total_fills = sum(c["fills"] for c in cycle_results)
    cycle_elapsed = [c["elapsed_s"] for c in cycle_results]

    print()
    print("=== AGGREGATE ===")
    print(f"Total cycles:        {args.n_cycles}")
    print(f"Total elapsed:       {total_elapsed:.1f}s")
    print(f"Mean cycle time:     {statistics.mean(cycle_elapsed):.3f}s")
    print(f"Stdev cycle time:     {statistics.stdev(cycle_elapsed):.3f}s")
    print(f"Max cycle time:      {max(cycle_elapsed):.3f}s")
    print(f"Min cycle time:      {min(cycle_elapsed):.3f}s")
    print(f"Total quotes:        {total_quotes}")
    print(f"Total cancels:       {total_cancels}")
    print(f"Total fills:         {total_fills}")
    print()
    if total_elapsed > 0:
        sim_hours = (args.n_cycles * 8.37)
        sim_speedup = sim_hours / (total_elapsed / 3600.0)
        print(f"Simulated activity:  {sim_hours:.1f}h")
        print(f"Wall-clock time:     {total_elapsed:.1f}s ({total_elapsed/3600:.2f}h)")
        print(f"Replay speedup:      {sim_speedup:.0f}x realtime")

    # Write report
    report = {
        "journal": str(journal_path),
        "profile": args.profile,
        "n_cycles": args.n_cycles,
        "cycle_results": cycle_results,
        "aggregate": {
            "total_elapsed_s": total_elapsed,
            "mean_cycle_s": statistics.mean(cycle_elapsed),
            "stdev_cycle_s": statistics.stdev(cycle_elapsed) if len(cycle_elapsed) > 1 else 0.0,
            "max_cycle_s": max(cycle_elapsed),
            "min_cycle_s": min(cycle_elapsed),
            "total_quotes": total_quotes,
            "total_cancels": total_cancels,
            "total_fills": total_fills,
        },
    }
    report_path = out_dir / "stress_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
