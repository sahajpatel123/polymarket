#!/usr/bin/env python3
"""Bundle C-01 / shadow-AS / regime evidence for a denser paper window.

Tier-1 ops: refreshes STRATEGY_CANDIDATES-facing numbers from the live tape
without merging any pricing change. Auto-infers tokens like replay_livecfg.

Usage:
  uv run python scripts/candidate_evidence_pack.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from polymaker.metrics.shadow_as import analyze_shadow_as
from polymaker.replay import discover_condition_ids, infer_yes_no_tokens


def _run(cmd: list[str]) -> dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    payload: dict[str, Any] = {
        "returncode": proc.returncode,
        "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-8:]),
    }
    text = proc.stdout.strip()
    # key=value gate / status scripts
    kv: dict[str, str] = {}
    for line in proc.stdout.splitlines() + proc.stderr.splitlines():
        if "=" in line and not line.startswith("{") and " " not in line.split("=", 1)[0]:
            k, _, v = line.partition("=")
            if k:
                kv[k] = v
        if line.startswith("status="):
            payload["status_line"] = line
    if kv:
        payload["kv"] = kv
    if text.startswith("{"):
        try:
            payload["json"] = json.loads(text)
            return payload
        except json.JSONDecodeError:
            pass
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload["json"] = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    payload["stdout_tail"] = "\n".join(text.splitlines()[-20:])
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--journal", default=None)
    ap.add_argument("--metrics-log", default=None)
    ap.add_argument("--baseline-profile", default="live-tiny")
    ap.add_argument("--knob", default="trend_vol_ratio")
    ap.add_argument("--values", default="2,5,8")
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--out", default="logs/candidate_evidence_pack.json")
    args = ap.parse_args()

    cfg = Path(args.config_dir)
    journal = Path(args.journal) if args.journal else cfg / "journal" / "paper.jsonl"
    metrics = (
        Path(args.metrics_log)
        if args.metrics_log
        else cfg / "logs" / "metrics-paper.jsonl"
    )
    if not journal.exists() or not metrics.exists():
        print(
            f"status=MISSING journal={journal.exists()} metrics={metrics.exists()}",
            file=sys.stderr,
        )
        return 2

    cids = discover_condition_ids(metrics)
    markets: list[dict[str, Any]] = []
    validate_rows: list[dict[str, Any]] = []

    for cid in cids:
        yes, no = infer_yes_no_tokens(metrics, cid)
        if not yes or not no:
            markets.append({"condition_id": cid, "error": "token_infer_failed"})
            continue
        markets.append({"condition_id": cid, "yes": yes, "no": no})
        with tempfile.TemporaryDirectory(prefix="evpack_") as td:
            # validate writes JSON to stdout
            cmd = [
                sys.executable,
                "scripts/validate_knob_candidate.py",
                "--journal",
                str(journal),
                "--config-dir",
                str(cfg),
                "--baseline-profile",
                args.baseline_profile,
                "--knob",
                args.knob,
                "--values",
                args.values,
                "--holdout-frac",
                str(args.holdout_frac),
                "--split",
                "events",
                "--yes-token",
                yes,
                "--no-token",
                no,
                "--condition-id",
                cid,
                "--tick-size",
                str(args.tick_size),
            ]
            # validate prints JSON to stdout — capture via temp redirect of cwd out
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            entry: dict[str, Any] = {
                "condition_id": cid,
                "returncode": proc.returncode,
                "status_line": next(
                    (ln for ln in proc.stderr.splitlines() if ln.startswith("status=")),
                    None,
                ),
            }
            try:
                entry["result"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                entry["stdout_tail"] = proc.stdout[-2000:]
                entry["stderr_tail"] = proc.stderr[-1000:]
            validate_rows.append(entry)

    shadow = analyze_shadow_as(metrics).as_dict()
    regime = _run([sys.executable, "scripts/paper_regime_report.py"])
    gate = _run([sys.executable, "scripts/paper_data_gate.py"])
    scorecard = _run([sys.executable, "scripts/reward_scorecard.py"])

    # Summarize best full-window delta and OOS flag across markets
    c01: dict[str, Any] = {"knob": args.knob, "values": args.values, "markets": []}
    any_oos = False
    for row in validate_rows:
        res = row.get("result") or {}
        full_rows = ((res.get("full") or {}).get("rows")) or []
        hold_rows = ((res.get("holdout") or {}).get("rows")) or []
        # pick value 8 if present else last
        pick = None
        for r in full_rows:
            if r.get("candidate_value") == 8 or r.get("candidate_value") == 8.0:
                pick = r
        if pick is None and full_rows:
            pick = full_rows[-1]
        hold_pick = None
        for r in hold_rows:
            if pick and r.get("candidate_value") == pick.get("candidate_value"):
                hold_pick = r
        if hold_pick is None and hold_rows:
            hold_pick = hold_rows[-1] if pick else None
        oos = bool(res.get("oos_replicated"))
        any_oos = any_oos or oos
        best = res.get("best_full") or pick
        hold_best = res.get("best_on_holdout") or hold_pick
        c01["markets"].append({
            "condition_id": row.get("condition_id"),
            "status_line": row.get("status_line"),
            "full_dn_quote": (best or {}).get("delta_n_quote"),
            "holdout_dn_quote": (hold_best or {}).get("delta_n_quote"),
            "oos_replicated": oos,
            "thin_holdout": bool(res.get("thin_holdout")),
            "best_value": (best or {}).get("candidate_value"),
        })
    c01["any_oos_replicated"] = any_oos

    pack = {
        "gate": gate.get("kv") or gate,
        "c01_trend_vol_ratio": c01,
        "shadow_adverse_selection": shadow,
        "regime": regime.get("status_line") or regime.get("kv") or regime.get("stderr_tail"),
        "scorecard": scorecard.get("status_line") or scorecard.get("kv"),
        "markets_inferred": markets,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps(pack, indent=2, sort_keys=True, default=str))

    sh = shadow
    gate_kv = gate.get("kv") or {}
    print(
        f"status=OK out={out} "
        f"tier2={gate_kv.get('tier2_allowed')} "
        f"c01_any_oos={any_oos} "
        f"shadow_lifetimes={sh.get('n_quote_lifetimes')} "
        f"crossed_frac={sh.get('crossed_frac')} "
        f"markout_30s={((sh.get('markout_mean') or {}).get('30s'))}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
