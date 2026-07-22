#!/usr/bin/env python3
"""One-shot strategy-loop snapshot: gate + metrics + reward/regime + synth A/B.

Tier-1 ops tool for Agent-1 cycles. Does not change strategy math.

Usage:
  uv run python scripts/strategy_snapshot.py
  uv run python scripts/strategy_snapshot.py --config-dir livecfg
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from polymaker.config import Config
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics.analyze import analyze
from polymaker.replay.compare import compare_profiles, load_named_profile
from polymaker.replay.synth import write_regime_journal

from scripts.paper_regime_report import analyze_paper_log
from scripts.reward_scorecard import build_scorecard


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.exists():
            return p
    return None


def _run_gate() -> dict:
    proc = subprocess.run(
        [sys.executable, "scripts/paper_data_gate.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    out: dict[str, str] = {"stdout": proc.stdout.strip(), "returncode": str(proc.returncode)}
    for line in proc.stdout.splitlines():
        if "=" in line and not line.startswith("{"):
            k, _, v = line.partition("=")
            if k and " " not in k:
                out[k] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--strategy-config-dir", default="config",
                    help="Where named profiles live (usually config/)")
    ap.add_argument("--baseline-profile", default="newsom-mm")
    ap.add_argument("--candidate-profile", default="romania-pm")
    args = ap.parse_args()

    metrics_path = _first_existing(
        Path(args.config_dir) / "logs" / "metrics-paper.jsonl",
        Path("livecfg/logs/metrics-paper.jsonl"),
        Path("logs/metrics-paper.jsonl"),
    )
    paper_log = _first_existing(
        Path(args.config_dir) / "logs" / "paper.jsonl",
        Path("livecfg/logs/paper.jsonl"),
        Path("logs/paper.jsonl"),
    )
    paper_metrics = None
    if metrics_path is not None:
        paper_metrics = analyze(metrics_path).as_dict()

    reward_card = None
    if metrics_path is not None:
        try:
            reward_card = build_scorecard(metrics_path, paper_log)
        except Exception as exc:  # noqa: BLE001
            reward_card = {"error": str(exc)}

    regime_rep = None
    if paper_log is not None:
        try:
            regime_rep = analyze_paper_log(paper_log)
        except Exception as exc:  # noqa: BLE001
            regime_rep = {"error": str(exc)}

    tmp_dir = Path("logs/strategy_snapshot")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    journal = tmp_dir / "regime_jump.jsonl"
    write_regime_journal(journal)
    meta = MarketMeta(
        condition_id="0xreplay",
        question="snapshot",
        slug="snapshot",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )
    try:
        baseline = load_named_profile(args.baseline_profile, config_dir=args.strategy_config_dir)
        candidate = load_named_profile(args.candidate_profile, config_dir=args.strategy_config_dir)
        compare = compare_profiles(
            journal, meta, baseline, candidate, tmp_dir / "compare"
        ).as_dict()
    except KeyError as exc:
        compare = {"error": str(exc)}

    live_profiles: list[str] = []
    try:
        cfg = Config.load(args.config_dir, load_env=False)
        live_profiles = sorted({e.profile for e in cfg.markets if e.enabled})
    except Exception as exc:  # noqa: BLE001
        live_profiles = [f"load_error:{exc}"]

    gate = _run_gate()
    payload = {
        "gate": {k: v for k, v in gate.items() if k != "stdout"},
        "gate_stdout": gate.get("stdout"),
        "live_enabled_profiles": live_profiles,
        "paper_metrics_path": str(metrics_path) if metrics_path else None,
        "paper_metrics": paper_metrics,
        "reward_scorecard": reward_card,
        "regime_report": regime_rep,
        "offline_compare": {
            "baseline_profile": args.baseline_profile,
            "candidate_profile": args.candidate_profile,
            "result": compare,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))

    g_status = gate.get("status", "?")
    g_tier2 = str(gate.get("tier2_allowed", "?")).split()[0]
    nq = (paper_metrics or {}).get("n_quote", 0)
    reward = (paper_metrics or {}).get("reward_accrual_usdc") or {}
    reward_sum = round(sum(float(v) for v in reward.values()), 6) if isinstance(reward, dict) else 0.0
    d_quote = None
    if isinstance(compare, dict) and isinstance(compare.get("delta"), dict):
        d_quote = compare["delta"].get("n_quote")
    trending_frac = (regime_rep or {}).get("trending_frac") if isinstance(regime_rep, dict) else None
    top_rph = None
    if isinstance(reward_card, dict) and reward_card.get("markets"):
        top_rph = reward_card["markets"][0].get("reward_per_hour_usdc")
    print(
        f"status=OK gate={g_status} tier2={g_tier2} paper_quotes={nq} "
        f"reward_accrual_sum={reward_sum} top_reward_per_hour={top_rph} "
        f"trending_frac={trending_frac} offline_dn_quote={d_quote}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
