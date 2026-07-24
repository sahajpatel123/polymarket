#!/usr/bin/env python3
"""Live monitor: watch the running 12h paper session and report metrics.

Does NOT restart the session. Reads the journal + metrics that the
running paper collector is producing, and prints a status report.

Usage:
  uv run python scripts/live_monitor.py
  uv run python scripts/live_monitor.py --once  # single report, no loop
  uv run python scripts/live_monitor.py --interval 300  # 5-min reports
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from polymaker.metrics.analyze import analyze


def _event_breakdown(journal_path: Path) -> dict[str, int]:
    """Count event types in the live journal."""
    if not journal_path.exists():
        return {}
    counts: dict[str, int] = {}
    with journal_path.open() as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = str(obj.get("kind", "?"))
            counts[k] = counts.get(k, 0) + 1
    return counts


def _quote_analysis(metrics_path: Path) -> dict:
    """Analyze the running paper session's metrics log."""
    if not metrics_path.exists():
        return {"healthy": False, "reason": "metrics file missing"}
    rep = analyze(metrics_path)
    return {
        "healthy": True,
        "n_quote": rep.n_quote,
        "n_cancel": rep.n_cancel,
        "n_fill": rep.n_fill,
        "n_mark": rep.n_mark,
        "n_dust_quotes": rep.n_dust_quotes,
        "n_oob_quotes": rep.n_oob_quotes,
        "n_in_band_quotes": rep.n_in_band_quotes,
        "in_band_seconds": dict(rep.in_band_seconds),
        "mean_resting_notional": dict(rep.mean_resting_notional_usdc),
        "reward_accrual_usdc": dict(rep.reward_accrual_usdc),
        "inventory_drift_abs_peak": rep.inventory_drift_abs_peak,
        "realized_spread_usdc": rep.realized_spread_usdc,
    }


def _health(journal_path: Path, metrics_path: Path, max_age_s: float = 120) -> dict:
    """Check the paper collector is alive and producing fresh data."""
    out: dict = {"healthy": True, "checks": {}}
    now = time.time()

    # Journal freshness
    if journal_path.exists():
        mtime = journal_path.stat().st_mtime
        age = now - mtime
        out["checks"]["journal_age_s"] = round(age, 1)
        out["checks"]["journal_ok"] = age < max_age_s
    else:
        out["checks"]["journal_ok"] = False
        out["checks"]["journal_age_s"] = None

    # Metrics freshness
    if metrics_path.exists():
        mtime = metrics_path.stat().st_mtime
        age = now - mtime
        out["checks"]["metrics_age_s"] = round(age, 1)
        out["checks"]["metrics_ok"] = age < max_age_s
    else:
        out["checks"]["metrics_ok"] = False
        out["checks"]["metrics_age_s"] = None

    out["healthy"] = out["checks"].get("journal_ok", False)
    return out


def _format_report(
    session_json: dict,
    health: dict,
    events: dict,
    quote: dict,
    runtime_s: float,
) -> str:
    """Format a human-readable status report."""
    start = session_json.get("start_iso", "?")
    end = session_json.get("planned_end_iso", "?")
    profile = session_json.get("profile", "?")
    bankroll = session_json.get("bankroll_usdc", "?")

    lines = []
    lines.append("=" * 70)
    lines.append(f"  LIVE PAPER SESSION MONITOR — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    lines.append("=" * 70)
    lines.append(f"  Session:    {start} → {end}")
    lines.append(f"  Elapsed:    {runtime_s / 3600:.2f}h / 12.00h")
    lines.append(f"  Profile:    {profile}  |  Bankroll: ${bankroll}")
    lines.append("")

    # Health
    lines.append("  HEALTH")
    chk = health.get("checks", {})
    j_ok = "✓" if chk.get("journal_ok") else "✗"
    m_ok = "✓" if chk.get("metrics_ok") else "✗"
    lines.append(f"    Journal fresh:  {j_ok}  ({chk.get('journal_age_s', '?')}s old)")
    lines.append(f"    Metrics fresh:  {m_ok}  ({chk.get('metrics_age_s', '?')}s old)")
    lines.append("")

    # Events
    lines.append("  JOURNAL EVENTS (raw WebSocket feed)")
    if events:
        for k, v in sorted(events.items(), key=lambda x: -x[1]):
            lines.append(f"    {k:>16}: {v:>5}")
    else:
        lines.append("    (no events yet)")
    lines.append("")

    # Quote quality
    if quote.get("healthy"):
        lines.append("  QUOTE QUALITY (our order book)")
        lines.append(f"    Total quotes:    {quote.get('n_quote', 0):>5}")
        lines.append(f"    Total cancels:   {quote.get('n_cancel', 0):>5}")
        lines.append(f"    In-band quotes:  {quote.get('n_in_band_quotes', 0):>5}")
        lines.append(f"    Dust quotes:     {quote.get('n_dust_quotes', 0):>5}  (should be 0)")
        lines.append(f"    OOB quotes:      {quote.get('n_oob_quotes', 0):>5}  (should be 0)")
        lines.append(f"    Actual fills:    {quote.get('n_fill', 0):>5}")
        lines.append("")

        ibs = quote.get("in_band_seconds", {})
        if ibs:
            lines.append("  IN-BAND UPTIME")
            for cid, secs in ibs.items():
                cid_short = cid[:16] + "..."
                lines.append(f"    {cid_short}: {secs:.1f}s")
        lines.append("")

        mrn = quote.get("mean_resting_notional", {})
        if mrn:
            lines.append("  MEAN RESTING NOTIONAL (USDC)")
            for cid, usdc in mrn.items():
                cid_short = cid[:16] + "..."
                lines.append(f"    {cid_short}: ${usdc:.2f}")
        lines.append("")

        ra = quote.get("reward_accrual_usdc", {})
        if ra:
            lines.append("  REWARD ACCRUAL (this session)")
            for cid, usdc in ra.items():
                cid_short = cid[:16] + "..."
                lines.append(f"    {cid_short}: ${usdc:.4f}")
        lines.append("")

        lines.append(f"  INVENTORY")
        lines.append(f"    Max abs drift:   {quote.get('inventory_drift_abs_peak', 0):.2f} shares")
        lines.append(f"    Realized spread: ${quote.get('realized_spread_usdc', 0):.4f}")
    else:
        lines.append(f"  QUOTE QUALITY: {quote.get('reason', 'unknown')}")

    lines.append("=" * 70)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-json", default="livecfg/12h_session.json")
    ap.add_argument("--journal", default="livecfg/journal/paper.jsonl")
    ap.add_argument("--metrics", default="livecfg/logs/metrics-paper.jsonl")
    ap.add_argument("--interval", type=float, default=300.0, help="seconds between reports")
    ap.add_argument("--once", action="store_true", help="print one report and exit")
    args = ap.parse_args()

    session_path = Path(args.session_json)
    if not session_path.exists():
        print(f"ERROR: session file not found: {session_path}", file=sys.stderr)
        return 1
    session = json.loads(session_path.read_text())
    start_ts = session.get("start_ts", 0.0)
    end_ts = session.get("planned_end_ts", 0.0)

    journal_path = Path(args.journal)
    metrics_path = Path(args.metrics)

    if args.once:
        runtime_s = time.time() - start_ts
        h = _health(journal_path, metrics_path)
        e = _event_breakdown(journal_path)
        q = _quote_analysis(metrics_path)
        print(_format_report(session, h, e, q, runtime_s))
        return 0

    # Loop mode
    while True:
        now = time.time()
        if now >= end_ts:
            print(f"\nSession ended at {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
            break
        runtime_s = now - start_ts
        h = _health(journal_path, metrics_path)
        e = _event_breakdown(journal_path)
        q = _quote_analysis(metrics_path)
        # Clear screen and print
        print("\033[2J\033[H", end="")
        print(_format_report(session, h, e, q, runtime_s))
        print(f"\n  Next report in {args.interval:.0f}s. Ctrl-C to stop.")
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
