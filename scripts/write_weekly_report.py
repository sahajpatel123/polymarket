#!/usr/bin/env python3
"""Overwrite WEEKLY_REPORT.md from this-cycle script outputs (Tier-1 ops).

Passive visibility for long unattended runs. Does not change strategy math.

Usage:
  uv run python scripts/write_weekly_report.py
  uv run python scripts/write_weekly_report.py --out WEEKLY_REPORT.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _status_line(stderr: str, stdout: str = "") -> str:
    for line in stderr.splitlines() + stdout.splitlines():
        if line.startswith("status="):
            return line
    return "status=UNKNOWN"


def _gate_block(stdout: str) -> str:
    keep = []
    for line in stdout.splitlines():
        if line.startswith("paper_data_gate") or line.startswith("{"):
            continue
        if "=" in line and not line.startswith(" "):
            keep.append(line)
    return "\n".join(keep) if keep else stdout.strip()


def _git_head() -> str:
    code, out, _ = _run(["git", "log", "-1", "--oneline"])
    return out.strip() if code == 0 else "unknown"


def _pid_line() -> str:
    code, out, _ = _run(["pgrep", "-fl", "polymaker run"])
    if code != 0 or not out.strip():
        return "(no polymaker run process)"
    return out.strip().splitlines()[0]


def _outage_status_block(path: Path) -> str:
    """Pretty-print logs/outage_status.json for the weekly report (T1-81)."""
    if not path.exists():
        return "(missing — run strategy_tick or outage_window_report --status-out)"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return "(invalid JSON)"
    keys = (
        "ts",
        "connectivity",
        "outage_open",
        "outage_total_h",
        "outage_alert",
        "outage_alert_severe",
        "outage_alert_prolonged",
        "outage_alert_critical",
        "outage_alert_imminent",
        "outage_alert_final",
        "outage_alert_critical_aged",
        "operator_mode",
        "operator_action",
        "hours_to_critical",
        "minutes_to_critical",
        "hours_to_imminent",
        "outage_started_at",
        "outage_critical_at",
        "outage_critical_since",
        "hours_past_critical",
        "minutes_past_critical",
        "recovery_smoke",
        "recovery_smoke_blockers",
        "outage_imminent_since",
        "hours_in_imminent",
        "runtime_h",
        "hours_to_tier2_gate",
        "quotes",
        "tier2_allowed",
        "gate_reason",
        "runtime_basis",
        "recovered",
        "deps_ok",
        "deps_bumps",
        "deps_flagged",
        "tape_frozen",
        "eta_paused",
        "last_requote_age_s",
        "last_quote_age_s",
        "last_requote_at",
        "last_quote_at",
        "health",
        "ensure_status",
        "collector_pid",
        "collector_pids",
        "n_cycles",
        "c01_status",
        "c01_blockers",
        "paper_log",
        "paper_log_files",
        "metrics_log",
    )
    lines = [f"{k}={data.get(k)}" for k in keys if k in data]
    return "\n".join(lines) if lines else json.dumps(data, indent=2, sort_keys=True)


def _count_changelog_tier1(path: Path) -> int:
    """Count Tier-1 changelog lines (protocol weekly visibility; T1-88)."""
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text().splitlines():
        if "| Tier1 |" in line or "| Tier 1 |" in line:
            n += 1
    return n


def _count_backlog_tier1_done(path: Path) -> int:
    """Count BACKLOG ### T1-* items marked Status: done."""
    if not path.exists():
        return 0
    text = path.read_text().splitlines()
    n = 0
    in_t1 = False
    for line in text:
        if line.startswith("## Tier 2"):
            break
        if line.startswith("### T1-"):
            in_t1 = True
            continue
        if in_t1 and line.startswith("- Status:"):
            if "`done`" in line:
                n += 1
            in_t1 = False
    return n


def _count_pending_reviews(path: Path) -> int:
    """Count non-empty PENDING_REVIEW table rows (T1-89)."""
    if not path.exists():
        return 0
    n = 0
    in_table = False
    for line in path.read_text().splitlines():
        if line.startswith("| Opened |"):
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("|---"):
            continue
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0].lower()
        if first.startswith("_(none)") or first == "(none)" or first == "":
            continue
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="WEEKLY_REPORT.md")
    ap.add_argument(
        "--outage-status",
        default="logs/outage_status.json",
        help="Compact outage/gate status JSON to embed",
    )
    args = ap.parse_args()
    py = sys.executable
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _, gate_out, _ = _run([py, "scripts/paper_data_gate.py"])
    _, _, metrics = _run([py, "scripts/paper_metrics.py"])
    _, _, shadow = _run([py, "scripts/shadow_adverse_selection.py"])
    _, _, regime = _run([py, "scripts/paper_regime_report.py"])
    _, _, c01 = _run([py, "scripts/c01_promotion_checklist.py"])
    _, _, summarize = _run([py, "scripts/summarize_strategy_cycles.py"])
    _, deps_out, deps_err = _run([py, "scripts/deps_audit.py"])
    deps_status = _status_line(deps_err, deps_out)
    try:
        deps_ok = json.loads(deps_out).get("ok")
    except json.JSONDecodeError:
        deps_ok = None

    head = _git_head()
    pid = _pid_line()
    c01_line = _status_line(c01)
    sum_line = _status_line(summarize)
    outage_path = Path(args.outage_status)
    outage_block = _outage_status_block(outage_path)
    tier1_changelog = _count_changelog_tier1(Path("CHANGELOG_AGENT.md"))
    tier1_backlog_done = _count_backlog_tier1_done(Path("BACKLOG.md"))
    pending_reviews = _count_pending_reviews(Path("PENDING_REVIEW.md"))

    body = f"""# WEEKLY_REPORT

Passive visibility for long-run unattended operation. Overwritten by the
autonomous loop. Not an action request.

## Week of {ts[:10]} (UTC)

Generated: `{ts}` (via `scripts/write_weekly_report.py`)

### System

| Item | Status |
|------|--------|
| Branch | `git log -1` → `{head}` |
| Paper trading | `{pid}` |
| Loop | 10m Agent-1 strategy-pricing cadence; Tier-2 gated on hours |
| Tier-1 changelog lines | `{tier1_changelog}` (from `CHANGELOG_AGENT.md`) |
| Tier-1 backlog done | `{tier1_backlog_done}` (from `BACKLOG.md` Status: done) |

### Tier-2 PRs

| Opened | Still pending |
|--------|----------------|
| {pending_reviews} | see `PENDING_REVIEW.md` |

Open candidates: `docs/STRATEGY_CANDIDATES.md` (C-01…C-04).

### Outage / gate snapshot

`{outage_path}`:

```
{outage_block}
```

### Paper P&L / risk metrics (literal script output)

`uv run python scripts/paper_data_gate.py`:

```
{_gate_block(gate_out)}
```

`uv run python scripts/paper_metrics.py`:

```
{_status_line(metrics)}
```

`uv run python scripts/shadow_adverse_selection.py`:

```
{_status_line(shadow)}
```

`uv run python scripts/paper_regime_report.py`:

```
{_status_line(regime)}
```

`uv run python scripts/c01_promotion_checklist.py`:

```
{c01_line}
```

`uv run python scripts/summarize_strategy_cycles.py`:

```
{sum_line}
```

### Dependency / security audit

`uv run python scripts/deps_audit.py`:

```
{deps_status}
ok={deps_ok}
```

### Credentials / certificates

No expiry tracker in-repo. `.env` is gitignored; operator must rotate
`PK` / builder creds outside this loop.

### Blockers (informational)

- Parse C-01 / summarize / outage_status above for outage_alert, tape_frozen,
  ETA pause, tier2_allowed, and promotion blockers. Do not promote Tier-2
  while health is STALE or holdouts are thin.
- Live capital / size increases remain human-only (`ESCALATE.md`).
"""
    path = Path(args.out)
    path.write_text(body)
    print(json.dumps({
        "wrote": str(path),
        "ts": ts,
        "head": head,
        "tier1_changelog": tier1_changelog,
        "tier1_backlog_done": tier1_backlog_done,
        "pending_reviews": pending_reviews,
    }, indent=2))
    print(
        f"status=OK wrote={path} ts={ts} "
        f"tier1_changelog={tier1_changelog} tier1_backlog_done={tier1_backlog_done} "
        f"pending_reviews={pending_reviews} "
        f"c01={c01_line.split()[0] if c01_line else '?'} "
        f"deps={deps_status.split()[0] if deps_status else '?'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
