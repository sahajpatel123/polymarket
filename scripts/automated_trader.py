#!/usr/bin/env python3
"""Automated trader orchestration: run paper/live + health checks + backtester.

This script orchestrates unattended operation of polymaker:
1. Runs health checks (connectivity, env, engine, paper, outage, PnL)
2. Optionally starts the engine (paper or live)
3. Periodically runs the backtester on recorded journal
4. Tracks PnL history
5. Alerts on issues

Usage:
  uv run python scripts/automated_trader.py --paper --once           # one-shot health check
  uv run python scripts/automated_trader.py --paper --interval 300   # loop every 5 min
  uv run python scripts/automated_trader.py --live --interval 600   # live, every 10 min
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def run_health_check(config_dir: str, state_db: str, paper_log: str,
                     outage_status: str, daily_loss_threshold: float) -> dict[str, Any]:
    """Run the market health check script."""
    result = subprocess.run(
        [sys.executable, "scripts/market_health.py", "--json",
         "--config-dir", config_dir,
         "--state-db", state_db,
         "--paper-log", paper_log,
         "--outage-status", outage_status,
         "--daily-loss-threshold", str(daily_loss_threshold)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"overall": "ERR", "err": f"health check failed: {result.stderr[:200]}"}


def run_backtest(journal: str, profile: str, config_dir: str, out_dir: str) -> dict[str, Any]:
    """Run the backtester on the latest journal."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / "latest_summary.json"
    result = subprocess.run(
        [sys.executable, "scripts/backtest.py",
         "--journal", journal,
         "--profile", profile,
         "--config-dir", config_dir,
         "--out-dir", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    if summary_path.exists():
        try:
            return json.loads(summary_path.read_text())
        except json.JSONDecodeError:
            return {"err": f"invalid summary: {result.stderr[:200]}"}
    return {"err": f"backtest failed: {result.stderr[:200]}"}


def ensure_engine_running(paper: bool, config_dir: str) -> bool:
    """Start the engine if not already running. Returns True if running."""
    # Check if engine is already running
    result = subprocess.run(
        ["pgrep", "-f", "polymaker run"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = [p for p in result.stdout.strip().split("\n") if p]
    if pids:
        return True

    # Start the engine
    mode = "--paper" if paper else ""
    log_path = Path("logs/automated_trader_engine.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["uv", "run", "polymaker", "run", "--config-dir", config_dir]
    if mode:
        cmd.append(mode)

    with log_path.open("a") as fh:
        subprocess.Popen(
            cmd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paper", action="store_true", help="run in paper mode")
    ap.add_argument("--live", action="store_true", help="run in live mode (DANGEROUS)")
    ap.add_argument("--once", action="store_true", help="run once and exit (no loop)")
    ap.add_argument("--interval", type=int, default=300, help="loop interval in seconds")
    ap.add_argument("--config-dir", default="config", help="config directory")
    ap.add_argument("--state-db", default="state.db", help="SQLite state DB")
    ap.add_argument("--paper-log", default="livecfg/logs/paper.jsonl", help="paper log path")
    ap.add_argument("--outage-status", default="logs/outage_status.json", help="outage status path")
    ap.add_argument("--journal", default="livecfg/logs/paper.jsonl", help="journal to backtest")
    ap.add_argument("--profile", default="political-longdated", help="strategy profile for backtest")
    ap.add_argument("--backtest-out", default="backtest_out/", help="backtest output dir")
    ap.add_argument("--daily-loss-threshold", type=float, default=40.0,
                    help="daily loss threshold (USDC)")
    ap.add_argument("--alert-webhook", default="", help="webhook URL for alerts")
    ap.add_argument("--auto-start-engine", action="store_true",
                    help="auto-start the engine if not running")
    ap.add_argument("--backtest-every", type=int, default=6,
                    help="run backtest every N health-check cycles")
    args = ap.parse_args()

    if not args.paper and not args.live:
        print("ERROR: must specify --paper or --live", file=sys.stderr)
        return 1

    if args.live:
        print("WARNING: live mode is DANGEROUS. Ensure you have validated via paper + backtester.")
        print("         Only proceed if scripts/backtest.py shows positive PnL on your target markets.")
        print()

    cycle = 0
    while True:
        cycle += 1
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"\n=== Cycle {cycle} at {ts} ===")

        # 1. Health check
        print("Running health check...")
        health = run_health_check(
            args.config_dir, args.state_db, args.paper_log,
            args.outage_status, args.daily_loss_threshold,
        )
        overall = health.get("overall", "UNKNOWN")
        print(f"  health: {overall}")
        if health.get("critical_issues"):
            print(f"  CRITICAL: {health['critical_issues']}")
        elif health.get("issues"):
            print(f"  issues: {health['issues']}")

        # 2. Auto-start engine if configured
        if args.auto_start_engine:
            was_running = ensure_engine_running(args.paper, args.config_dir)
            if not was_running:
                print("  engine: started")
            else:
                print("  engine: already running")

        # 3. Periodic backtest
        if cycle % args.backtest_every == 0:
            print("Running backtest...")
            bt = run_backtest(args.journal, args.profile, args.config_dir, args.backtest_out)
            if "err" in bt:
                print(f"  backtest: FAILED ({bt['err'][:100]})")
            else:
                n_results = len(bt.get("results", []))
                total_est = sum(r.get("pnl_estimate", {}).get("total_est_usdc", 0)
                                for r in bt.get("results", []))
                print(f"  backtest: {n_results} market(s), total_est=${total_est:.4f}")

        # 4. Alert on critical issues
        if args.alert_webhook and overall in ("CRITICAL", "DEGRADED"):
            try:
                import httpx
                msg = f"polymaker automated: {overall}\n"
                if health.get("critical_issues"):
                    msg += f"CRITICAL: {health['critical_issues']}\n"
                if health.get("issues"):
                    msg += f"issues: {health['issues']}"
                httpx.post(args.alert_webhook, json={
                    "title": "polymaker automated health",
                    "message": msg,
                    "critical": overall == "CRITICAL",
                    "ts": time.time(),
                }, timeout=10.0)
            except Exception as exc:  # noqa: BLE001
                print(f"  alert send failed: {exc}", file=sys.stderr)

        if args.once:
            break

        print(f"Sleeping {args.interval}s...")
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
