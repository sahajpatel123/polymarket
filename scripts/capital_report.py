#!/usr/bin/env python3
"""Run a $30-capital backtest report.

Runs the existing backtester against the recorded journal, then shows:
- What $30 of paper money would have done
- Per-market breakdown
- 24h projection from the recorded window
- Whether $30 is sufficient for the strategy's max inventory

Usage:
  uv run python scripts/capital_report.py --capital 30 --journal livecfg/journal/paper.jsonl
  uv run python scripts/capital_report.py --capital 30 --profile newsom-mm
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _load_journal_window(journal_path: Path) -> tuple[float, float, int]:
    """Return (ts_start, ts_end, n_rows) of the journal."""
    ts_start = ts_end = 0.0
    n = 0
    with journal_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(obj.get("ts") or 0.0)
            if ts <= 0:
                continue
            if ts_start == 0.0 or ts < ts_start:
                ts_start = ts
            if ts > ts_end:
                ts_end = ts
            n += 1
    return ts_start, ts_end, n


def _format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m:02d}m"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capital", type=float, default=30.0, help="total capital in USDC")
    ap.add_argument("--journal", default="livecfg/journal/paper.jsonl", help="journal to backtest")
    ap.add_argument("--profile", default="political-longdated", help="strategy profile")
    ap.add_argument("--config-dir", default="config", help="config directory")
    ap.add_argument("--out-dir", default="/tmp/capital_report", help="backtest output dir")
    ap.add_argument("--db", default="/tmp/capital_report_catalog.db", help="catalog DB path")
    args = ap.parse_args()

    journal_path = Path(args.journal)
    if not journal_path.exists():
        print(f"ERROR: journal not found: {journal_path}", file=sys.stderr)
        return 1

    # 1. Show the journal window
    ts_start, ts_end, n_rows = _load_journal_window(journal_path)
    window_s = ts_end - ts_start
    print("=" * 70)
    print(f"  $ {args.capital:.0f} PAPER-MONEY BACKTEST REPORT")
    print("=" * 70)
    print(f"\n  Journal:       {journal_path}")
    print(f"  Strategy:      {args.profile}")
    print(f"  Window:        {_format_duration(window_s)} ({n_rows:,} events)")
    print(f"  From:          {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(ts_start))}")
    print(f"  To:            {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(ts_end))}")

    # 2. Run the backtest
    import subprocess

    db_path = Path(args.db)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / "backtest_summary.json"

    result = subprocess.run(
        [
            sys.executable, "scripts/backtest.py",
            "--journal", str(journal_path),
            "--profile", args.profile,
            "--config-dir", args.config_dir,
            "--out-dir", str(out_dir),
            "--db", str(db_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not summary_path.exists():
        print(f"ERROR: backtest failed: {result.stderr[:200]}", file=sys.stderr)
        return 1

    summary = json.loads(summary_path.read_text())
    results = summary.get("results", [])
    if not results:
        print("ERROR: no results in backtest summary", file=sys.stderr)
        return 1

    # 3. Aggregate results
    total_spread = 0.0
    total_reward = 0.0
    total_rebate = 0.0
    total_fills = 0
    total_quotes = 0
    total_cancels = 0
    max_inventory = 0.0
    worst_markout = 0.0
    per_market = []

    for r in results:
        m = r.get("metrics", {})
        p = r.get("pnl_estimate", {})
        n_fill = m.get("n_fill", 0)
        n_quote = m.get("n_quote", 0)
        n_cancel = m.get("n_cancel", 0)
        spread = p.get("spread_usdc", 0.0)
        reward = p.get("reward_usdc", 0.0)
        rebate = p.get("rebate_est_usdc", 0.0)
        inv_peak = m.get("inventory_drift_abs_peak", 0.0)
        markout_mean = m.get("markout", {}).get("30s", 0.0)
        cid = r.get("condition_id", "")

        total_spread += spread
        total_reward += reward
        total_rebate += rebate
        total_fills += n_fill
        total_quotes += n_quote
        total_cancels += n_cancel
        if inv_peak > max_inventory:
            max_inventory = inv_peak
        if markout_mean < worst_markout:
            worst_markout = markout_mean
        per_market.append({
            "cid": cid[:16] + "...",
            "fills": n_fill,
            "quotes": n_quote,
            "cancels": n_cancel,
            "spread": spread,
            "markout": markout_mean,
            "inv_peak": inv_peak,
        })

    total_pnl = total_spread + total_reward + total_rebate
    capital_sufficient = max_inventory <= args.capital

    # 4. Print the $30 report
    print(f"\n{'─' * 70}")
    print("  PER-MARKET BREAKDOWN")
    print(f"{'─' * 70}")
    for pm in per_market:
        print(
            f"  {pm['cid']:<20} fills={pm['fills']:>3}  "
            f"spread=${pm['spread']:>+7.4f}  "
            f"markout={pm['markout']:>+7.4f}  "
            f"inv_peak=${pm['inv_peak']:>7.2f}"
        )

    print(f"\n{'─' * 70}")
    print(f"  $ {args.capital:.0f} CAPITAL ASSESSMENT")
    print(f"{'─' * 70}")
    print(f"  Total fills:        {total_fills}")
    print(f"  Total quotes:       {total_quotes}")
    print(f"  Total cancels:      {total_cancels}")
    print(f"  Max inventory held: ${max_inventory:,.2f}")
    if capital_sufficient:
        print(f"  ✅ $ {args.capital:.0f} is SUFFICIENT (max inv ${max_inventory:.2f} < capital)")
    else:
        print(f"  ⚠️  $ {args.capital:.0f} is INSUFFICIENT (max inv ${max_inventory:.2f} > capital)")
        print(f"      Reduce base_size_usdc or increase capital to ${max_inventory:.0f}+")

    print(f"\n{'─' * 70}")
    print(f"  PnL OVER RECORDED WINDOW ({_format_duration(window_s)})")
    print(f"{'─' * 70}")
    print(f"  Spread PnL:    ${total_spread:>+8.4f}")
    print(f"  Reward PnL:    ${total_reward:>+8.4f}  (liquidity rewards)")
    print(f"  Rebate est:    ${total_rebate:>+8.4f}  (maker rebates)")
    print("  ─────────────────────────")
    print(f"  TOTAL PnL:     ${total_pnl:>+8.4f}")
    print(f"  Return on $ {args.capital:.0f}:  {(total_pnl / args.capital) * 100:>+7.4f}%")
    print(f"  Worst markout: {worst_markout:>+7.4f} per share (30s horizon)")

    # 5. Project to 24h
    if window_s > 0:
        pnl_per_hour = total_pnl / (window_s / 3600.0)
        fills_per_hour = total_fills / (window_s / 3600.0)
        projected_24h = pnl_per_hour * 24
        projected_fills_24h = fills_per_hour * 24
        print(f"\n{'─' * 70}")
        print(f"  24-HOUR PROJECTION (linear extrapolation from {_format_duration(window_s)} window)")
        print(f"{'─' * 70}")
        print(f"  PnL/hour:           ${pnl_per_hour:>+8.4f}/h")
        print(f"  Fills/hour:         {fills_per_hour:>5.1f}/h")
        print(f"  Projected 24h PnL:  ${projected_24h:>+8.4f}")
        print(f"  Projected 24h fills: {projected_fills_24h:>5.1f}")
        print(f"  Return on $ {args.capital:.0f}:  {(projected_24h / args.capital) * 100:>+7.4f}%")
        print()
        print("  ⚠️  This is a LINEAR projection from "
              f"{_format_duration(window_s)} of data.")
        print("     Real 24h results depend on market activity, volatility, and fills.")
        print("     Run paper mode for 24h to validate.")

    # 6. Save report
    report_path = out_dir / "capital_report.json"
    report_path.write_text(json.dumps({
        "capital_usdc": args.capital,
        "journal": str(journal_path),
        "profile": args.profile,
        "window_s": window_s,
        "total_fills": total_fills,
        "total_spread": total_spread,
        "total_reward": total_reward,
        "total_rebate": total_rebate,
        "total_pnl": total_pnl,
        "max_inventory": max_inventory,
        "capital_sufficient": capital_sufficient,
        "per_market": per_market,
        "projected_24h_pnl": pnl_per_hour * 24 if window_s > 0 else 0,
        "projected_24h_fills": fills_per_hour * 24 if window_s > 0 else 0,
    }, indent=2, default=str))
    print(f"\n  Report saved to: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
