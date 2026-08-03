#!/usr/bin/env python3
"""Generate a 12h paper-session return report.

Reads the session marker written at start, slices metrics/journal by start_ts,
runs paper metrics + offline backtest on the collected tape, writes a markdown
report. Invoked automatically when the 12h paper session ends.

Usage:
  uv run python scripts/paper_12h_report.py --session livecfg/12h_session.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _slice_jsonl(src: Path, dst: Path, start_ts: float) -> int:
    """Copy lines with ts >= start_ts (or no ts) into dst. Returns line count."""
    n = 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open() as fin, dst.open("w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = obj.get("ts")
            if ts is not None:
                try:
                    if float(ts) < start_ts - 1.0:
                        continue
                except (TypeError, ValueError):
                    pass
            fout.write(line + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", default="livecfg/12h_session.json")
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    session_path = repo / args.session
    if not session_path.exists():
        print(f"ERROR: session file missing: {session_path}", file=sys.stderr)
        return 1

    session = json.loads(session_path.read_text())
    start_ts = float(session["start_ts"])
    bankroll = float(session.get("bankroll_usdc", 100.0))
    config_dir = session.get("config_dir", "livecfg")
    profile = session.get("profile", "live_scaled")
    end_ts = time.time()
    elapsed_h = (end_ts - start_ts) / 3600.0

    report_dir = repo / session.get("report_dir", "livecfg/12h_report")
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    log_dir = repo / config_dir / "logs"
    journal_dir = repo / config_dir / "journal"
    paper_log = log_dir / "paper.jsonl"
    metrics_log = log_dir / "metrics-paper.jsonl"
    journal_log = journal_dir / "paper.jsonl"

    slice_dir = report_dir / f"slice_{stamp}"
    slice_dir.mkdir(parents=True, exist_ok=True)

    n_paper = n_metrics = n_journal = 0
    if paper_log.exists():
        n_paper = _slice_jsonl(paper_log, slice_dir / "paper.jsonl", start_ts)
    if metrics_log.exists():
        n_metrics = _slice_jsonl(metrics_log, slice_dir / "metrics-paper.jsonl", start_ts)
    if journal_log.exists():
        n_journal = _slice_jsonl(journal_log, slice_dir / "journal.jsonl", start_ts)

    # Offline backtest on collected journal
    backtest_out = slice_dir / "backtest"
    backtest_out.mkdir(exist_ok=True)
    bt_rc, bt_out, bt_err = 1, "", ""
    if n_journal > 0:
        bt_rc, bt_out, bt_err = _run(
            [
                sys.executable,
                "scripts/backtest.py",
                "--journal",
                str(slice_dir / "journal.jsonl"),
                "--profile",
                profile,
                "--config-dir",
                config_dir,
                "--bankroll",
                str(bankroll),
                "--db",
                str(slice_dir / "empty.db"),
                "--metrics-source",
                str(slice_dir / "metrics-paper.jsonl"),
                "--out-dir",
                str(backtest_out),
            ],
            repo,
        )
    (slice_dir / "backtest_stdout.txt").write_text(bt_out + "\n" + bt_err)

    # Paper metrics analyze if we have metrics
    pm_out = ""
    if n_metrics > 0:
        _, pm_out, pm_err = _run(
            [
                sys.executable,
                "scripts/paper_metrics.py",
                "--log",
                str(slice_dir / "metrics-paper.jsonl"),
            ],
            repo,
        )
        pm_out = (pm_out + "\n" + pm_err).strip()
        (slice_dir / "paper_metrics.txt").write_text(pm_out)

    # Gate / health
    gate_out = ""
    _, g_out, g_err = _run(
        [sys.executable, "scripts/paper_data_gate.py"],
        repo,
    )
    gate_out = (g_out + "\n" + g_err).strip()
    (slice_dir / "paper_data_gate.txt").write_text(gate_out)

    # Load backtest summary if present
    summary: dict = {}
    sum_path = backtest_out / "backtest_summary.json"
    if sum_path.exists():
        summary = json.loads(sum_path.read_text())

    # Connectivity snapshot
    _, c_out, c_err = _run([sys.executable, "scripts/polymarket_connectivity.py"], repo)
    conn = (c_out + "\n" + c_err).strip()

    md = []
    md.append(f"# 12h Paper Validation Report")
    md.append("")
    md.append(f"Generated: `{stamp}` UTC")
    md.append(f"Session start: `{session.get('start_iso', start_ts)}`")
    md.append(f"Elapsed wall hours: **{elapsed_h:.2f}** (target 12.0)")
    md.append(f"Profile: `{profile}` · bankroll: **${bankroll:.2f}** · config: `{config_dir}`")
    md.append("")
    md.append("## Data collected")
    md.append("")
    md.append(f"| Stream | Lines since start |")
    md.append(f"|--------|------------------|")
    md.append(f"| paper.jsonl | {n_paper} |")
    md.append(f"| metrics-paper.jsonl | {n_metrics} |")
    md.append(f"| journal/paper.jsonl | {n_journal} |")
    md.append("")
    md.append("## Offline backtest on collected journal")
    md.append("")
    if summary:
        md.append(f"| Metric | Value |")
        md.append(f"|--------|-------|")
        md.append(f"| runtime_hours (journal) | {summary.get('runtime_hours')} |")
        md.append(f"| total_est_usdc | {summary.get('total_est_usdc')} |")
        md.append(f"| period_return_pct | {summary.get('period_return_pct')} |")
        md.append(f"| daily_return_pct | {summary.get('daily_return_pct')} |")
        md.append(f"| n_fill | {summary.get('n_fill')} |")
        md.append(f"| estimate_is_reward_only | {summary.get('estimate_is_reward_only')} |")
        md.append(f"| reward_pool_usdc | {summary.get('reward_pool_usdc')} |")
        md.append(f"| reward_our_usdc | {summary.get('reward_our_usdc')} |")
        md.append(f"| spread_usdc | {summary.get('spread_usdc')} |")
        oob = summary.get("oob_check") or {}
        md.append(f"| oob_check.ok | {oob.get('ok')} (quotes={oob.get('quotes')}, dust={oob.get('dust_le_0.001')}, oob={oob.get('oob')}) |")
        md.append("")
        if summary.get("estimate_is_reward_only"):
            md.append(
                "> **NOTE:** 0 fills in this window — returns are share-adjusted "
                "reward accrual only, not fill PnL. Paper fill sim still needs trade prints."
            )
            md.append("")
    else:
        md.append(f"Backtest did not produce a summary (rc={bt_rc}).")
        md.append("```")
        md.append((bt_out + bt_err)[-3000:])
        md.append("```")
        md.append("")

    md.append("## Paper metrics (session slice)")
    md.append("```")
    md.append(pm_out[-2000:] if pm_out else "(no metrics)")
    md.append("```")
    md.append("")
    md.append("## Paper data gate")
    md.append("```")
    md.append(gate_out[-1500:] if gate_out else "(none)")
    md.append("```")
    md.append("")
    md.append("## Connectivity at report time")
    md.append("```")
    md.append(conn[:1500] if conn else "(none)")
    md.append("```")
    md.append("")
    md.append("## How to interpret")
    md.append("")
    md.append("- `period_return_pct` = total_est / bankroll over the **observed journal window**.")
    md.append("- `daily_return_pct` = period / (runtime_h/24) — extrapolation, not a calendar day guarantee.")
    md.append("- Runtime uses journal activity timestamps (not wall-clock).")
    md.append("- Compare to theoretical offline A/B on the same markets; large gaps mean tape/connectivity issues.")
    md.append("")
    md.append(f"Artifacts: `{slice_dir.relative_to(repo)}/`")

    report_md = report_dir / f"REPORT_{stamp}.md"
    report_md.write_text("\n".join(md) + "\n")
    # Also write a stable latest pointer
    latest = report_dir / "REPORT_LATEST.md"
    latest.write_text(report_md.read_text())

    session["end_ts"] = end_ts
    session["end_iso"] = datetime.now(timezone.utc).isoformat()
    session["elapsed_h"] = elapsed_h
    session["report_path"] = str(report_md.relative_to(repo))
    session["status"] = "completed"
    session_path.write_text(json.dumps(session, indent=2))

    print(f"status=OK report={report_md} elapsed_h={elapsed_h:.2f}")
    print(report_md.read_text()[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
