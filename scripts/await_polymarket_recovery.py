#!/usr/bin/env python3
"""Poll Polymarket connectivity and relaunch paper collector when it recovers.

Tier-1 ops for long outages (REST/WS DOWN). Does not change strategy math.

On recovery (default): restart the paper collector, then append one strategy
cycle so the longitudinal trail timestamps the outage→UP transition.

Also refreshes ``logs/outage_status.json`` on each probe (STILL_DOWN or
RECOVERED) so monitors stay current without a full strategy_tick.

Usage:
  uv run python scripts/await_polymarket_recovery.py --once
  uv run python scripts/await_polymarket_recovery.py --interval-s 60 --max-wait-s 3600
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _status_line(stderr: str, stdout: str = "") -> str:
    for line in (stderr.splitlines() + stdout.splitlines()):
        if line.startswith("status="):
            return line
    return "status=UNKNOWN"


def _refresh_outage_status(py: str, status_out: str) -> str:
    code, out, err = _run([
        py,
        "scripts/outage_window_report.py",
        "--status-out",
        status_out,
    ])
    return _status_line(err, out)


def _patch_outage_status(status_out: str, **fields: object) -> None:
    """Merge fields into the compact outage status JSON (T1-78/T1-79)."""
    path = Path(status_out)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    data.update({"ts": datetime.now(timezone.utc).isoformat(), **fields})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _mark_recovered(
    status_out: str,
    *,
    connectivity: str,
    waited_s: float,
    recovery_smoke: str | None = None,
    recovery_smoke_blockers: str | None = None,
) -> None:
    fields: dict = dict(
        recovered=True,
        outage_open=False,
        outage_alert=False,
        outage_alert_severe=False,
        outage_alert_prolonged=False,
        outage_alert_critical=False,
        outage_alert_imminent=False,
        outage_alert_final=False,
        outage_imminent_since=None,
        hours_in_imminent=None,
        outage_critical_since=None,
        hours_past_critical=None,
        minutes_past_critical=None,
        outage_started_at=None,
        outage_critical_at=None,
        hours_to_critical=12.0,
        minutes_to_critical=720,
        hours_to_imminent=11.0,
        last_requote_at=None,
        last_quote_at=None,
        connectivity=connectivity,
        waited_s=waited_s,
    )
    if recovery_smoke is not None:
        fields["recovery_smoke"] = recovery_smoke
    if recovery_smoke_blockers is not None:
        fields["recovery_smoke_blockers"] = recovery_smoke_blockers
    _patch_outage_status(status_out, **fields)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--timeout-s", type=float, default=5.0)
    ap.add_argument("--interval-s", type=float, default=60.0)
    ap.add_argument("--max-wait-s", type=float, default=0.0,
                    help="0 = poll forever until UP or --once")
    ap.add_argument("--once", action="store_true",
                    help="Single connectivity check (no loop)")
    ap.add_argument("--no-restart-on-recover", action="store_true",
                    help="Do not relaunch collector when connectivity returns")
    ap.add_argument(
        "--no-append-cycle-on-recover",
        action="store_true",
        help="Skip append_strategy_cycle after a successful recovery",
    )
    ap.add_argument(
        "--no-smoke-on-recover",
        action="store_true",
        help="Skip recovery_smoke.py after a successful recovery (T1-107)",
    )
    ap.add_argument(
        "--status-out",
        default="logs/outage_status.json",
        help="Compact outage status JSON path (empty string disables)",
    )
    ap.add_argument("--wait-s", type=float, default=45.0,
                    help="ensure_paper_collector post-restart health wait")
    ap.add_argument(
        "--smoke-min-quotes",
        type=int,
        default=None,
        help="Pass through to recovery_smoke --min-quotes",
    )
    args = ap.parse_args()
    restart = not args.no_restart_on_recover
    append_cycle = not args.no_append_cycle_on_recover
    run_smoke = not args.no_smoke_on_recover
    status_out = (args.status_out or "").strip()

    py = sys.executable
    t0 = time.time()
    attempt = 0
    while True:
        attempt += 1
        code, out, err = _run([
            py, "scripts/polymarket_connectivity.py",
            "--timeout-s", str(args.timeout_s),
        ])
        line = _status_line(err, out)
        ts = datetime.now(timezone.utc).isoformat()
        print(json.dumps({
            "ts": ts,
            "attempt": attempt,
            "connectivity_rc": code,
            "connectivity": line,
        }), flush=True)

        if code == 0 and "status=OK" in line:
            report = {
                "ts": ts,
                "recovered": True,
                "attempts": attempt,
                "waited_s": round(time.time() - t0, 1),
                "connectivity": line,
            }
            if restart:
                rcode, rout, rerr = _run([
                    py, "scripts/ensure_paper_collector.py",
                    "--config-dir", args.config_dir,
                    "--restart",
                    "--force-restart",
                    "--wait-s", str(args.wait_s),
                ])
                report["ensure_rc"] = rcode
                report["ensure"] = _status_line(rerr, rout)
            if append_cycle:
                acode, aout, aerr = _run([
                    py,
                    "scripts/append_strategy_cycle.py",
                    "--with-counterfactual",
                ])
                report["append_rc"] = acode
                report["append"] = _status_line(aerr, aout)
            if status_out:
                report["outage_status"] = _refresh_outage_status(py, status_out)
                # Diagnose probes (no restart/append/smoke) must not clear
                # outage_open — strategy_tick uses that mode (T1-108).
                diagnose_only = not restart and not append_cycle and not run_smoke
                if diagnose_only:
                    _patch_outage_status(
                        status_out,
                        connectivity=line,
                        recovered=False,
                    )
                else:
                    _mark_recovered(
                        status_out,
                        connectivity=line,
                        waited_s=float(report["waited_s"]),
                    )
            smoke_line = "skipped"
            if run_smoke and status_out:
                smoke_cmd = [py, "scripts/recovery_smoke.py", "--status", status_out]
                if args.smoke_min_quotes is not None:
                    smoke_cmd.extend(["--min-quotes", str(args.smoke_min_quotes)])
                scode, sout, serr = _run(smoke_cmd)
                smoke_line = _status_line(serr, sout)
                report["smoke_rc"] = scode
                report["smoke"] = smoke_line
                blockers = "-"
                for part in smoke_line.split():
                    if part.startswith("blockers="):
                        blockers = part.partition("=")[2]
                        break
                _patch_outage_status(
                    status_out,
                    recovery_smoke="PASS" if scode == 0 else "FAIL",
                    recovery_smoke_blockers=blockers,
                )
            print(json.dumps(report, indent=2, sort_keys=True))
            if not restart and not append_cycle and not run_smoke:
                print(
                    f"status=UP_DIAGNOSE waited_s={report['waited_s']} "
                    f"ensure=skipped append=skipped smoke=skipped "
                    f"status_out={status_out or '-'}",
                    file=sys.stderr,
                )
                return 0
            print(
                f"status=RECOVERED waited_s={report['waited_s']} "
                f"ensure={report.get('ensure', 'skipped')} "
                f"append={report.get('append', 'skipped')} "
                f"smoke={smoke_line} "
                f"status_out={status_out or '-'}",
                file=sys.stderr,
            )
            ok = True
            if restart and report.get("ensure_rc") != 0:
                ok = False
            if append_cycle and report.get("append_rc") != 0:
                ok = False
            if run_smoke and status_out and report.get("smoke_rc") not in (0, None):
                ok = False
            return 0 if ok else 1

        if status_out:
            _refresh_outage_status(py, status_out)
            _patch_outage_status(
                status_out,
                connectivity=line,
                recovered=False,
            )

        if args.once:
            print(
                f"status=STILL_DOWN {line} status_out={status_out or '-'}",
                file=sys.stderr,
            )
            return 1

        if args.max_wait_s > 0 and (time.time() - t0) >= args.max_wait_s:
            print(
                f"status=TIMEOUT waited_s={round(time.time() - t0, 1)} {line} "
                f"status_out={status_out or '-'}",
                file=sys.stderr,
            )
            return 1

        time.sleep(max(1.0, args.interval_s))


if __name__ == "__main__":
    raise SystemExit(main())
