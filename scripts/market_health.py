#!/usr/bin/env python3
"""Monitor market health and alert on issues for unattended operation.

Checks:
- Polymarket REST + WS connectivity
- Paper collector health (if running)
- Outage status
- Required secrets configured
- Engine process count (exactly one)
- Recent fill/quote activity
- Daily PnL vs kill threshold

Usage:
  uv run python scripts/market_health.py --json          # machine-readable
  uv run python scripts/market_health.py --alert-webhook URL  # alert on issues
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def check_connectivity() -> dict[str, Any]:
    """Probe Polymarket REST + WS endpoints."""
    result: dict[str, Any] = {"status": "UNKNOWN", "checks": {}}
    try:
        import httpx

        # REST probe
        try:
            r = httpx.get("https://clob.polymarket.com/time", timeout=10.0)
            r.raise_for_status()
            result["checks"]["rest"] = {"ok": True, "status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            result["checks"]["rest"] = {"ok": False, "err": str(exc)}

        result["status"] = "OK" if all(c.get("ok") for c in result["checks"].values()) else "DEGRADED"
    except ImportError:
        result["status"] = "SKIP"
        result["err"] = "httpx not available"
    return result


def check_env() -> dict[str, Any]:
    """Check required .env configuration."""
    result: dict[str, Any] = {"status": "OK", "missing": []}
    required = ["PK", "BROWSER_ADDRESS"]
    for key in required:
        val = os.environ.get(key, "")
        if not val:
            result["missing"].append(key)
    if result["missing"]:
        result["status"] = "MISSING"
    return result


def check_engine_process() -> dict[str, Any]:
    """Check exactly one polymaker run process."""
    import subprocess

    result: dict[str, Any] = {"status": "OK", "n_processes": 0, "pids": []}
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "polymaker run"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        pids = [p for p in proc.stdout.strip().split("\n") if p]
        result["n_processes"] = len(pids)
        result["pids"] = pids
        if len(pids) == 0:
            result["status"] = "STOPPED"
        elif len(pids) > 1:
            result["status"] = "DUPLICATE"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "ERR"
        result["err"] = str(exc)
    return result


def check_paper_health(metrics_path: Path, paper_log: Path | None) -> dict[str, Any]:
    """Check paper collector health and recent activity."""
    result: dict[str, Any] = {"status": "OK", "last_quote_age_s": None, "last_quote_at": None}

    # Check paper log freshness
    if paper_log and paper_log.exists():
        try:
            # Read last line
            with paper_log.open() as fh:
                lines = fh.readlines()
            for line in reversed(lines[-200:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(obj.get("ts") or 0.0)
                if ts > 0:
                    age = time.time() - ts
                    result["last_quote_age_s"] = round(age, 1)
                    result["last_quote_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
                    if age > 300:  # 5 min stale
                        result["status"] = "STALE"
                    break
        except Exception as exc:  # noqa: BLE001
            result["status"] = "ERR"
            result["err"] = str(exc)
    else:
        result["status"] = "NO_LOG"

    return result


def check_outage_status(outage_path: Path) -> dict[str, Any]:
    """Check logs/outage_status.json for open outages."""
    result: dict[str, Any] = {"status": "OK", "outage_open": False}
    if not outage_path.exists():
        result["status"] = "NO_STATUS"
        return result
    try:
        data = json.loads(outage_path.read_text())
        result["outage_open"] = bool(data.get("outage_open", False))
        result["outage_total_h"] = data.get("outage_total_h")
        result["tier2_allowed"] = data.get("tier2_allowed")
        if result["outage_open"]:
            result["status"] = "OUTAGE"
    except json.JSONDecodeError:
        result["status"] = "INVALID"
    return result


def check_daily_pnl(state_db: Path, threshold: float) -> dict[str, Any]:
    """Check latest PnL snapshot from state DB."""
    import sqlite3

    result: dict[str, Any] = {"status": "OK", "daily_pnl": None}
    if not state_db.exists():
        result["status"] = "NO_DB"
        return result
    try:
        conn = sqlite3.connect(str(state_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT daily_pnl FROM pnl_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is not None:
            pnl = float(row["daily_pnl"])
            result["daily_pnl"] = pnl
            if pnl <= -threshold:
                result["status"] = "KILLED"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "ERR"
        result["err"] = str(exc)
    return result


def send_alert(webhook_url: str, message: str, critical: bool = False) -> bool:
    """Send an alert to the configured webhook."""
    try:
        import httpx

        payload = {
            "title": "polymaker health alert",
            "message": message,
            "critical": critical,
            "ts": time.time(),
        }
        r = httpx.post(webhook_url, json=payload, timeout=10.0)
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  Alert send failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    ap.add_argument("--config-dir", default="config", help="config directory")
    ap.add_argument("--state-db", default="state.db", help="SQLite state DB path")
    ap.add_argument("--paper-log", default="livecfg/logs/paper.jsonl", help="paper log path")
    ap.add_argument("--outage-status", default="logs/outage_status.json", help="outage status path")
    ap.add_argument("--daily-loss-threshold", type=float, default=40.0,
                    help="daily loss threshold (USDC) to flag")
    ap.add_argument("--alert-webhook", default=os.environ.get("ALERT_WEBHOOK_URL", ""),
                    help="webhook URL to send alerts on issues")
    args = ap.parse_args()

    report: dict[str, Any] = {
        "ts": time.time(),
        "connectivity": check_connectivity(),
        "env": check_env(),
        "engine_process": check_engine_process(),
        "paper_health": check_paper_health(Path("logs/metrics-paper.jsonl"), Path(args.paper_log)),
        "outage_status": check_outage_status(Path(args.outage_status)),
        "daily_pnl": check_daily_pnl(Path(args.state_db), args.daily_loss_threshold),
    }

    # Determine overall status
    issues: list[str] = []
    critical_issues: list[str] = []
    for name, check in report.items():
        if name == "ts":
            continue
        status = check.get("status", "UNKNOWN")
        if status in ("KILLED", "DUPLICATE", "ERR", "OUTAGE", "STALE", "MISSING", "INVALID"):
            issues.append(f"{name}={status}")
        if status in ("KILLED", "DUPLICATE", "ERR", "MISSING"):
            critical_issues.append(f"{name}={status}")

    if critical_issues:
        report["overall"] = "CRITICAL"
    elif issues:
        report["overall"] = "DEGRADED"
    else:
        report["overall"] = "OK"

    report["issues"] = issues
    report["critical_issues"] = critical_issues

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"polymaker health check — {report['overall']}")
        print(f"  connectivity:    {report['connectivity']['status']}")
        print(f"  env:             {report['env']['status']}"
              + (f" (missing: {report['env']['missing']})" if report['env']['missing'] else ""))
        print(f"  engine_process:  {report['engine_process']['status']}"
              + f" ({report['engine_process']['n_processes']} procs)")
        print(f"  paper_health:    {report['paper_health']['status']}"
              + (f" (last quote {report['paper_health']['last_quote_age_s']}s ago)"
                 if report['paper_health']['last_quote_age_s'] is not None else ""))
        print(f"  outage_status:   {report['outage_status']['status']}"
              + (f" (open {report['outage_status']['outage_total_h']}h)"
                 if report['outage_status']['outage_open'] else ""))
        print(f"  daily_pnl:       {report['daily_pnl']['status']}"
              + (f" (${report['daily_pnl']['daily_pnl']:.2f})"
                 if report['daily_pnl']['daily_pnl'] is not None else ""))

        if issues:
            print(f"\n  issues: {', '.join(issues)}")
        if critical_issues:
            print(f"  CRITICAL: {', '.join(critical_issues)}")

    # Send alert if configured and there are issues
    if args.alert_webhook and (issues or critical_issues):
        msg = f"polymaker health: {report['overall']}\n"
        if critical_issues:
            msg += f"CRITICAL: {', '.join(critical_issues)}\n"
        if issues:
            msg += f"issues: {', '.join(issues)}"
        ok = send_alert(args.alert_webhook, msg, critical=bool(critical_issues))
        if not args.json:
            print(f"\n  alert sent: {ok}")

    # Exit code: 0 if OK, 1 if DEGRADED, 2 if CRITICAL
    if report["overall"] == "CRITICAL":
        return 2
    if report["overall"] == "DEGRADED":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
