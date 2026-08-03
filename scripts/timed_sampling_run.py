#!/usr/bin/env python3
"""Time-boxed paper session that collects online fill samples, then stops itself.

Runs the engine for a fixed wall-clock budget, snapshots progress to disk on an
interval, restarts the engine if it dies, and on expiry stops it cleanly (which
flushes the online-sample checkpoint) and writes a final report.

Everything needed to analyse the run afterwards is left on disk, so nothing has
to be watched live:

    <out>/timeline.jsonl   periodic snapshots (fills, samples, PnL, governor)
    <out>/events.jsonl     supervisor events (start/restart/stop/expiry)
    <out>/FINAL.json       end-of-run summary incl. realized round trips
    <out>/FINAL.md         same, human readable

Usage:
    uv run python scripts/timed_sampling_run.py \
        --config-dir session3 --hours 4 --out session3/report
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def round_trips(db: Path) -> list[dict[str, Any]]:
    """FIFO-match SELLs against BUYs -> realized round trips."""
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = list(con.execute(
            "SELECT token_id, side, price, size, ts FROM fills ORDER BY ts, rowid"))
        con.close()
    except sqlite3.Error:
        return []
    lots: dict[str, deque[list[float]]] = {}
    out: list[dict[str, Any]] = []
    for tok, side, px, sz, ts in rows:
        px, sz, ts = float(px), float(sz), float(ts)
        dq = lots.setdefault(tok, deque())
        if str(side) == "BUY":
            dq.append([sz, px, ts])
            continue
        rem = sz
        while rem > 1e-9 and dq:
            lot = dq[0]
            take = min(rem, lot[0])
            out.append({"token": tok, "qty": take, "buy": lot[1], "sell": px,
                        "pnl": (px - lot[1]) * take, "hold_s": ts - lot[2]})
            lot[0] -= take
            rem -= take
            if lot[0] <= 1e-9:
                dq.popleft()
    return out


def online_sample_progress(model_dir: Path) -> tuple[int, int]:
    """Return (all online rows, actual filled rows) from the durable sidecar."""
    p = model_dir / "fill_online_samples.pkl"
    if not p.exists():
        return (0, 0)
    try:
        import pickle
        with p.open("rb") as fh:
            blob = pickle.load(fh)
        y_fill = blob["y_fill"]
        return (int(len(blob["features"])), int(sum(float(v) for v in y_fill)))
    except Exception:
        return (0, 0)


def online_sample_count(model_dir: Path) -> int:
    """Backward-compatible total-row count."""
    return online_sample_progress(model_dir)[0]


def snapshot(db: Path, model_dir: Path, log: Path) -> dict[str, Any]:
    snap: dict[str, Any] = {"ts": time.time(),
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S")}
    fills: dict[str, int] = {}
    orders: dict[str, int] = {}
    equity = None
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            fills = {str(s): int(n) for s, n in con.execute(
                "select side,count(*) from fills group by side")}
            orders = {str(s): int(n) for s, n in con.execute(
                "select side,count(*) from order_log group by side")}
            r = con.execute(
                "select equity,net_cash,inventory_value from pnl_snapshots "
                "order by ts desc limit 1").fetchone()
            if r:
                equity = {"equity": round(float(r[0]), 4),
                          "net_cash": round(float(r[1]), 4),
                          "inventory_value": round(float(r[2]), 4)}
            con.close()
        except sqlite3.Error as exc:
            snap["db_error"] = str(exc)
    rt = round_trips(db)
    wins = [t for t in rt if t["pnl"] > 0]
    online_rows, online_fills = online_sample_progress(model_dir)
    snap.update({
        "fills": fills,
        "orders": orders,
        "online_samples": online_rows,
        "online_fill_samples": online_fills,
        "round_trips": len(rt),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(rt), 4) if rt else None,
        "realized_pnl": round(sum(t["pnl"] for t in rt), 4),
        "equity": equity,
    })
    if log.exists():
        try:
            text = log.read_text(errors="replace")
            snap["log_counts"] = {
                k: text.count(k) for k in (
                    "fill_labels_resolved", "round_trip_closed",
                    "online_samples_checkpointed", "fill_model_retrained",
                    "daily loss kill", "position_divergence", "quoter_error",
                )
            }
        except OSError:
            pass
    return snap


@dataclass
class Supervisor:
    config_dir: str
    hours: float
    out: Path
    deadline_epoch: float | None = None
    interval_s: float = 300.0
    max_restarts: int = 20
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        from polymaker.config import Config
        cfg = Config.load(self.config_dir)
        self.db = Path(cfg.paths.db)
        self.model_dir = Path(cfg.paths.model_dir)
        self.log = Path(cfg.paths.log_dir) / "stdout.log"
        self.min_online_rows = int(cfg.model.min_live_validation_samples)
        self.min_online_fills = int(cfg.model.min_live_fills)
        self.out.mkdir(parents=True, exist_ok=True)
        self.timeline = self.out / "timeline.jsonl"
        self.events_path = self.out / "events.jsonl"

    def _spawn(self) -> subprocess.Popen[bytes]:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        fh = self.log.open("ab")
        proc = subprocess.Popen(
            [str(ROOT / ".venv/bin/polymaker"), "run", "--paper", "--no-open",
             "--config-dir", self.config_dir],
            stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            start_new_session=True, cwd=str(ROOT),
        )
        self._event("engine_started", pid=proc.pid)
        return proc

    def _stop(self, proc: subprocess.Popen[bytes]) -> None:
        """SIGTERM so the engine's shutdown() flushes the sample checkpoint."""
        if proc.poll() is not None:
            return
        self._event("engine_stopping", pid=proc.pid)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        for _ in range(60):           # up to 30s for a clean flush
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is None:
            self._event("engine_kill_forced", pid=proc.pid)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    def _event(self, kind: str, **kw: Any) -> None:
        ev = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "event": kind, **kw}
        self.events.append(ev)
        with self.events_path.open("a") as fh:
            fh.write(json.dumps(ev) + "\n")
        print(f"[{ev['iso']}] {kind} {kw}", flush=True)

    def run(self) -> int:
        deadline = self.deadline_epoch or (time.time() + self.hours * 3600.0)
        rows_start, fills_start = online_sample_progress(self.model_dir)
        self._event("run_begin", hours=self.hours,
                    deadline_iso=time.strftime("%Y-%m-%dT%H:%M:%S",
                                               time.localtime(deadline)),
                    config_dir=self.config_dir,
                    samples_at_start=rows_start,
                    fill_samples_at_start=fills_start)
        proc = self._spawn()
        restarts = 0
        try:
            while time.time() < deadline:
                time.sleep(min(self.interval_s, max(1.0, deadline - time.time())))
                snap = snapshot(self.db, self.model_dir, self.log)
                snap["remaining_min"] = round((deadline - time.time()) / 60, 1)
                with self.timeline.open("a") as fh:
                    fh.write(json.dumps(snap) + "\n")
                print(f"  samples={snap['online_samples']} "
                      f"fill_samples={snap['online_fill_samples']} "
                      f"fills={snap['fills']} trips={snap['round_trips']} "
                      f"wr={snap['win_rate']} pnl={snap['realized_pnl']} "
                      f"left={snap['remaining_min']}m", flush=True)
                if proc.poll() is not None:
                    # Samples are checkpointed, so a restart loses nothing.
                    self._event("engine_died", code=proc.returncode,
                                restarts=restarts)
                    if restarts >= self.max_restarts:
                        self._event("giving_up", restarts=restarts)
                        break
                    restarts += 1
                    proc = self._spawn()
        except KeyboardInterrupt:
            self._event("interrupted")
        finally:
            self._stop(proc)
            rows_end, fills_end = online_sample_progress(self.model_dir)
            self._event("run_end", restarts=restarts,
                        samples_at_end=rows_end, fill_samples_at_end=fills_end)
            self._final_report()
        return 0

    def _final_report(self) -> None:
        rt = round_trips(self.db)
        wins = [t for t in rt if t["pnl"] > 0]
        losses = [t for t in rt if t["pnl"] <= 0]
        snap = snapshot(self.db, self.model_dir, self.log)
        gross_w = sum(t["pnl"] for t in wins)
        gross_l = sum(t["pnl"] for t in losses)

        def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
            if n == 0:
                return (float("nan"), float("nan"))
            p = k / n
            d = 1 + z * z / n
            c = (p + z * z / (2 * n)) / d
            h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
            return max(0.0, c - h), min(1.0, c + h)

        lo, hi = wilson(len(wins), len(rt))
        report = {
            "config_dir": self.config_dir,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "planned_hours": self.hours,
            "online_samples": snap["online_samples"],
            "online_fill_samples": snap["online_fill_samples"],
            "gate_target": self.min_online_rows,
            "gate_fill_target": self.min_online_fills,
            "gate_reached": (
                snap["online_samples"] >= self.min_online_rows
                and snap["online_fill_samples"] >= self.min_online_fills
            ),
            "fills": snap["fills"],
            "orders": snap["orders"],
            "equity": snap["equity"],
            "round_trips": {
                "closed": len(rt),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(rt), 4) if rt else None,
                "win_rate_ci95": [None, None] if not rt
                else [round(lo, 4), round(hi, 4)],
                "realized_pnl": round(sum(t["pnl"] for t in rt), 4),
                "mean_per_trip": round(sum(t["pnl"] for t in rt) / len(rt), 4)
                if rt else None,
                "gross_win": round(gross_w, 4),
                "gross_loss": round(gross_l, 4),
                "profit_factor": round(gross_w / abs(gross_l), 3)
                if gross_l else None,
                "median_hold_min": round(
                    sorted(t["hold_s"] for t in rt)[len(rt) // 2] / 60, 1)
                if rt else None,
            },
            "log_counts": snap.get("log_counts", {}),
            "events": self.events,
            "trips": rt,
        }
        (self.out / "FINAL.json").write_text(json.dumps(report, indent=2))

        r = report["round_trips"]
        md = [
            f"# Timed sampling run — {report['config_dir']}",
            "",
            f"Generated {report['generated']} · planned {self.hours}h",
            "",
            "## Online sample gate",
            f"- rows: **{report['online_samples']}** / {report['gate_target']}",
            f"- actual fills: **{report['online_fill_samples']}** / {report['gate_fill_target']}",
            f"- gate reached (both required): **{report['gate_reached']}**",
            "",
            "## Fills",
            f"- {report['fills']}",
            f"- orders: {report['orders']}",
            f"- equity: {report['equity']}",
            "",
            "## Realized round trips",
            f"- closed: **{r['closed']}** ({r['wins']}W / {r['losses']}L)",
            f"- win rate: **{r['win_rate']}**  CI95 {r['win_rate_ci95']}",
            f"- realized PnL: **${r['realized_pnl']}**",
            f"- mean per trip: ${r['mean_per_trip']}",
            f"- profit factor: **{r['profit_factor']}**",
            f"- median hold: {r['median_hold_min']} min",
            "",
            "## Log counters",
            *[f"- {k}: {v}" for k, v in report["log_counts"].items()],
            "",
        ]
        (self.out / "FINAL.md").write_text("\n".join(md))
        print("\n".join(md), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", required=True)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--deadline-epoch", type=float,
                    help="absolute stop time; preserves a prior run's deadline")
    ap.add_argument("--interval-min", type=float, default=5.0)
    args = ap.parse_args()
    sup = Supervisor(config_dir=args.config_dir, hours=args.hours,
                     out=args.out, deadline_epoch=args.deadline_epoch,
                     interval_s=args.interval_min * 60.0)
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())
