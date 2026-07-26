#!/usr/bin/env python3
"""Quantitative Edge evidence eval (calibration + OOS + significance).

Runs baseline vs candidate on one journal and prints the evidence package
required before any quant-edge technique counts as "implemented":

  - Brier / log-loss / ECE (proper scoring, not accuracy)
  - EV per quote net of adverse selection
  - Tune vs holdout (OOS) windows
  - Bootstrap CI + paired significance on chunked EV deltas

Usage:
  uv run python scripts/quant_edge_eval.py \\
      --journal fixtures/regime_dense.jsonl \\
      --candidate-overrides '{"use_advanced_quoting": true}'

  uv run python scripts/quant_edge_eval.py \\
      --journal livecfg/journal/paper.jsonl \\
      --config-dir livecfg --baseline-profile live-tiny \\
      --candidate-overrides '{"use_advanced_quoting": true}' \\
      --holdout-frac 0.3 --n-chunks 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.replay.compare import load_named_profile, profile_from_overrides
from polymaker.replay.quant_edge import evaluate_quant_edge, write_report


def _default_meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xreplay",
        question="quant-edge fixture",
        slug="quant-edge-fixture",
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


def _load_overrides(raw: str | None, path: str | None) -> dict:
    if path:
        obj = json.loads(Path(path).read_text())
        if not isinstance(obj, dict):
            raise SystemExit(f"overrides file must be a JSON object: {path}")
        return obj
    if raw:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise SystemExit("--*-overrides must be a JSON object")
        return obj
    return {}


def _meta_from_args(args: argparse.Namespace) -> MarketMeta:
    from dataclasses import replace

    meta = _default_meta()
    updates: dict = {}
    if args.condition_id:
        updates["condition_id"] = args.condition_id
    if args.tick_size is not None:
        updates["tick_size"] = args.tick_size
    if args.yes_token or args.no_token:
        yes = args.yes_token or "yes-token"
        no = args.no_token or "no-token"
        updates["tokens"] = (TokenMeta(yes, "Yes"), TokenMeta(no, "No"))
    return replace(meta, **updates) if updates else meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--out-dir", default="logs/quant_edge_eval")
    ap.add_argument("--report", default="logs/quant_edge_eval/report.json")
    ap.add_argument("--config-dir", default="config")
    ap.add_argument("--baseline-profile", default=None)
    ap.add_argument("--candidate-profile", default=None)
    ap.add_argument("--baseline-overrides", default=None)
    ap.add_argument("--candidate-overrides", default=None)
    ap.add_argument("--baseline-overrides-file", default=None)
    ap.add_argument("--candidate-overrides-file", default=None)
    ap.add_argument("--holdout-frac", type=float, default=0.3)
    ap.add_argument("--split", choices=("time", "events"), default="events")
    ap.add_argument("--n-chunks", type=int, default=5)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument(
        "--fill-mode",
        choices=("conservative", "base", "optimistic"),
        default="conservative",
        help="Replay fill model; conservative is promotion default",
    )
    ap.add_argument("--yes-token", default=None)
    ap.add_argument("--no-token", default=None)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=None)
    args = ap.parse_args()

    base_ov = _load_overrides(args.baseline_overrides, args.baseline_overrides_file)
    cand_ov = _load_overrides(args.candidate_overrides, args.candidate_overrides_file)

    if args.baseline_profile:
        baseline = load_named_profile(
            args.baseline_profile, config_dir=args.config_dir, overrides=base_ov
        )
    else:
        baseline = profile_from_overrides(StrategyProfile(), base_ov)

    if args.candidate_profile:
        candidate = load_named_profile(
            args.candidate_profile, config_dir=args.config_dir, overrides=cand_ov
        )
    else:
        # Default candidate: same baseline + advanced quoting on
        if not cand_ov and not args.candidate_profile:
            cand_ov = {"use_advanced_quoting": True}
        candidate = profile_from_overrides(baseline, cand_ov)

    meta = _meta_from_args(args)
    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    result = evaluate_quant_edge(
        journal,
        meta,
        baseline,
        candidate,
        Path(args.out_dir),
        holdout_frac=args.holdout_frac,
        split=args.split,
        n_chunks=args.n_chunks,
        alpha=args.alpha,
        fill_mode=args.fill_mode,
    )
    report_path = write_report(result, Path(args.report))
    d = result.as_dict()
    v = d["verdict"]
    sig = d["significance"]

    print(f"status=OK journal={journal} fill_mode={args.fill_mode}")
    print(f"report={report_path}")
    print(
        "full "
        f"dn_ev={d['full']['delta'].get('ev_per_quote_usdc')} "
        f"dn_brier={d['full']['delta'].get('brier_score')} "
        f"dn_logloss={d['full']['delta'].get('log_loss')} "
        f"dn_ece={d['full']['delta'].get('expected_calibration_error')} "
        f"n_fill={d['full']['baseline'].get('n_fill')}->{d['full']['candidate'].get('n_fill')}"
    )
    print(
        "holdout "
        f"dn_ev={d['holdout']['delta'].get('ev_per_quote_usdc')} "
        f"dn_brier={d['holdout']['delta'].get('brier_score')} "
        f"mode={d['holdout']['window'].get('mode')}"
    )
    print(
        "significance "
        f"n_chunks={sig.get('n_chunks')} "
        f"mean_delta={sig.get('bootstrap_mean_delta')} "
        f"ci=[{sig.get('bootstrap_ci_lower')},{sig.get('bootstrap_ci_upper')}] "
        f"p={sig.get('paired_p')} significant={sig.get('is_significant')}"
    )
    print(
        "verdict "
        f"finding={v.get('finding')} "
        f"promotion_eligible={v.get('promotion_eligible')} "
        f"oos_sign_match={v.get('oos_sign_match')} "
        f"ci_excludes_zero={v.get('ci_excludes_zero')}"
    )
    wired = sum(1 for t in d["inventory"] if t.get("wired") not in ("no",))
    evidenced = sum(1 for t in d["inventory"] if t.get("evidence") == "yes")
    print(f"inventory techniques={len(d['inventory'])} wired_or_partial={wired} evidence_yes={evidenced}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
