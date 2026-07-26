#!/usr/bin/env python3
"""Through-price tape + join reward-uptime tradeoff.

1) Classify SELL aggressors vs book best bid (through / at-touch / above).
2) Compare in_reward_band quote fraction for baseline vs join+min_edge0.

Diagnostic only — does not change live defaults.

Usage:
  uv run python scripts/through_price_tape.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --slug will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \\
      --db livecfg/state.db --config-dir livecfg
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from polymaker.replay import filter_rows_for_tokens, load_journal, run_replay
from polymaker.replay.compare import (
    load_named_profile,
    profile_from_overrides,
    write_sliced_journal,
)
from polymaker.replay.market_resolve import resolve_market_by_slug
from polymaker.replay.through_price_tape import measure_through_price_tape


def _in_band_frac(metrics_path: Path) -> dict[str, Any]:
    n = 0
    n_in = 0
    with metrics_path.open() as fh:
        for line in fh:
            e = json.loads(line)
            if e.get("event") != "quote":
                continue
            n += 1
            if e.get("in_reward_band"):
                n_in += 1
    return {
        "n_quote": n,
        "n_in_band": n_in,
        "in_band_frac": (n_in / n if n else 0.0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--db", default="livecfg/state.db")
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument("--report", default="logs/through_price_tape/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    meta = resolve_market_by_slug(args.slug, db_path=args.db)
    rows = filter_rows_for_tokens(
        load_journal(journal),
        yes_token=meta.yes.token_id,
        no_token=meta.no.token_id,
    )
    tape = measure_through_price_tape(rows, meta)

    base = load_named_profile(args.profile, config_dir=args.config_dir)
    join = profile_from_overrides(
        base, {"join_best_bid": True, "min_edge_ticks": 0}
    )
    reward: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="thru_") as td:
        root = Path(td)
        jpath = write_sliced_journal(rows, root / "j.jsonl")
        for label, prof in [("baseline", base), ("join_minedge0", join)]:
            mpath = root / f"{label}.jsonl"
            rr = run_replay(jpath, meta, prof, mpath, fill_mode="optimistic")
            band = _in_band_frac(mpath)
            reward[label] = {
                **band,
                "n_fill": rr.n_fill,
                "n_quote_replay": rr.n_quote,
            }

    report = {
        "slug": args.slug,
        "tape": tape.as_dict(),
        "reward_uptime": reward,
        "note": (
            "conservative_join_viable requires n_through>0. "
            "Reward uptime compares in_reward_band quote fraction."
        ),
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    t = tape.as_dict()
    print(
        f"status=OK through={t['n_through']} at_touch={t['n_at_touch']} "
        f"above={t['n_above_touch']} viable={t['conservative_join_viable']} "
        f"reason={t['reason']}"
    )
    print(
        f"reward baseline_in_band={reward['baseline']['in_band_frac']:.4f} "
        f"join_in_band={reward['join_minedge0']['in_band_frac']:.4f} "
        f"report={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
