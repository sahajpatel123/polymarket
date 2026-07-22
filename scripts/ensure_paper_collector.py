#!/usr/bin/env python3
"""Ensure the paper collector is advancing; optionally restart if STALE.

Tier-1 ops for long unattended runs toward the 24h Tier-2 gate. Does not
change strategy math. Default is diagnose-only; pass ``--restart`` to kill
``polymaker run --paper`` processes and relaunch.

When Polymarket REST/WS is DOWN, ``--restart`` is refused by default
(``SKIPPED_UPSTREAM_DOWN``) so outages do not thrash relaunches. Use
``--allow-down-restart`` to override; recovery helper already waits for UP.

Usage:
  uv run python scripts/ensure_paper_collector.py
  uv run python scripts/ensure_paper_collector.py --restart --config-dir livecfg
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _paper_health(
    max_age_s: float,
    *,
    paper_log: str | None = None,
    metrics_log: str | None = None,
) -> tuple[int, dict]:
    cmd = [sys.executable, "scripts/paper_health.py", "--max-age-s", str(max_age_s)]
    if paper_log:
        cmd.extend(["--paper-log", paper_log])
    if metrics_log:
        cmd.extend(["--metrics-log", metrics_log])
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload: dict = {}
    text = proc.stdout.strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
    status = next(
        (ln for ln in proc.stderr.splitlines() if ln.startswith("status=")),
        f"status=UNKNOWN rc={proc.returncode}",
    )
    payload["_status_line"] = status
    payload["_rc"] = proc.returncode
    return proc.returncode, payload


def _find_paper_pids() -> list[int]:
    try:
        out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_s, _, cmd = line.partition(" ")
            pid = int(pid_s)
        except ValueError:
            continue
        if "polymaker" in cmd and "run" in cmd and "--paper" in cmd:
            if "ensure_paper_collector" in cmd:
                continue
            pids.append(pid)
    return sorted(set(pids))


def _stop_pids(pids: list[int], grace_s: float = 5.0) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace_s
    while time.time() < deadline:
        alive = [p for p in _find_paper_pids() if p in pids]
        if not alive:
            return
        time.sleep(0.2)
    for pid in _find_paper_pids():
        if pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _polymaker_bin() -> str:
    cand = Path(sys.executable).resolve().parent / "polymaker"
    return str(cand) if cand.exists() else "polymaker"


def _collector_log_hint(log_path: Path, *, tail: int = 80) -> str | None:
    """Best-effort diagnosis from recent collector stdout."""
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text(errors="replace").splitlines()[-tail:]
    except OSError:
        return None
    for line in reversed(lines):
        if "timed out during opening handshake" in line:
            return "ws_handshake_timeout"
        if "market_ws_subscribed" in line:
            return "ws_subscribed_seen"
        if "engine_started" in line:
            return "engine_started_seen"
    return None


def _upstream_ok(*, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Return (ok, status_line) from polymarket_connectivity probe."""
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/polymarket_connectivity.py",
            "--timeout-s",
            str(timeout_s),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    line = next(
        (ln for ln in (proc.stderr.splitlines() + proc.stdout.splitlines()) if ln.startswith("status=")),
        f"status=UNKNOWN rc={proc.returncode}",
    )
    return proc.returncode == 0 and "status=OK" in line, line


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--max-age-s", type=float, default=300.0)
    ap.add_argument("--paper-log", default=None)
    ap.add_argument("--metrics-log", default=None)
    ap.add_argument(
        "--restart",
        action="store_true",
        help="If health is STALE/missing, stop paper collectors and relaunch",
    )
    ap.add_argument(
        "--force-restart",
        action="store_true",
        help="Restart even when health is OK",
    )
    ap.add_argument(
        "--allow-down-restart",
        action="store_true",
        help="Allow restart even when Polymarket REST/WS probe is DOWN",
    )
    ap.add_argument(
        "--skip-connectivity-check",
        action="store_true",
        help="Skip upstream probe (tests / offline). Prefer --allow-down-restart for ops.",
    )
    ap.add_argument(
        "--connectivity-timeout-s",
        type=float,
        default=5.0,
    )
    ap.add_argument(
        "--wait-s",
        type=float,
        default=45.0,
        help="After restart, wait this long then re-check health",
    )
    args = ap.parse_args()

    cfg = Path(args.config_dir)
    (cfg / "journal").mkdir(parents=True, exist_ok=True)
    (cfg / "logs").mkdir(parents=True, exist_ok=True)

    rc, health = _paper_health(
        args.max_age_s, paper_log=args.paper_log, metrics_log=args.metrics_log
    )
    pids = _find_paper_pids()
    report = {
        "health_rc": rc,
        "health_status": health.get("_status_line"),
        "last_requote_age_s": health.get("last_requote_age_s"),
        "n_quote": health.get("n_quote"),
        "pids": pids,
        "action": "none",
    }

    need = args.force_restart or rc != 0 or not pids
    if not need:
        print(json.dumps(report, indent=2, sort_keys=True))
        print(
            f"status=OK health={health.get('_status_line')} pids={pids}",
            file=sys.stderr,
        )
        return 0

    if not args.restart and not args.force_restart:
        print(json.dumps(report, indent=2, sort_keys=True))
        print(
            f"status=NEEDS_RESTART health={health.get('_status_line')} pids={pids}",
            file=sys.stderr,
        )
        return 1

    # Avoid thrashing relaunches while Polymarket itself is unreachable (T1-59).
    if not args.skip_connectivity_check and not args.allow_down_restart:
        up_ok, up_line = _upstream_ok(timeout_s=args.connectivity_timeout_s)
        report["connectivity"] = up_line
        if not up_ok:
            report["action"] = "skipped_upstream_down"
            print(json.dumps(report, indent=2, sort_keys=True))
            print(
                f"status=SKIPPED_UPSTREAM_DOWN connectivity={up_line} "
                f"health={health.get('_status_line')} pids={pids}",
                file=sys.stderr,
            )
            return 2

    report["action"] = "restart"
    log_path = cfg / "logs" / "collector-stdout.log"
    if pids:
        _stop_pids(pids)
        report["stopped_pids"] = pids
        time.sleep(1.0)

    stdout = open(log_path, "a")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            _polymaker_bin(),
            "run",
            "--paper",
            "--config-dir",
            str(cfg),
        ],
        stdout=stdout,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file = cfg / "paper_collector.pid"
    pid_file.write_text(str(proc.pid) + "\n")
    report["started_pid"] = proc.pid
    report["pid_file"] = str(pid_file)
    report["collector_log"] = str(log_path)

    if args.wait_s > 0:
        time.sleep(args.wait_s)
        rc2, health2 = _paper_health(
            args.max_age_s, paper_log=args.paper_log, metrics_log=args.metrics_log
        )
        report["post_health_rc"] = rc2
        report["post_health_status"] = health2.get("_status_line")
        report["post_last_requote_age_s"] = health2.get("last_requote_age_s")
        hint = _collector_log_hint(log_path)
        report["collector_hint"] = hint
        print(json.dumps(report, indent=2, sort_keys=True))
        if rc2 != 0:
            print(
                f"status=RESTARTED_STILL_STALE health={health2.get('_status_line')} "
                f"hint={hint} pid={proc.pid}",
                file=sys.stderr,
            )
            return 1
        print(
            f"status=RESTARTED_OK health={health2.get('_status_line')} "
            f"hint={hint} pid={proc.pid}",
            file=sys.stderr,
        )
        return 0

    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"status=RESTARTED pid={proc.pid}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
