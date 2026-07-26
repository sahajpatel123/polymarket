#!/usr/bin/env python3
"""Quote side coverage: YES vs NO resting quotes vs where the tape trades.

Usage:
  uv run python scripts/quote_side_coverage.py \\
      --journal … --config-dir livecfg --profile live_scaled \\
      --yes-token … --no-token … --condition-id …
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from polymaker.domain import MarketMeta, Side, TokenMeta
from polymaker.marketdata.parse import parse_last_trade
from polymaker.metrics import MetricsLogger
from polymaker.replay import (
    ReplayState,
    _recompute,
    apply_journal_event,
    filter_rows_for_tokens,
    load_journal,
    run_replay,
)
from polymaker.replay.compare import load_named_profile, write_sliced_journal
from polymaker.replay.token_pair_sanity import assess_token_pair


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument("--config-dir", default="livecfg")
    ap.add_argument("--profile", default="live_scaled")
    ap.add_argument("--yes-token", required=True)
    ap.add_argument("--no-token", required=True)
    ap.add_argument("--condition-id", default=None)
    ap.add_argument("--tick-size", type=float, default=0.001)
    ap.add_argument("--report", default="logs/quote_side_coverage/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    yes, no = args.yes_token, args.no_token
    pair = assess_token_pair(journal, yes, no)
    meta = MarketMeta(
        condition_id=args.condition_id or "0xreplay",
        question="side-cov",
        slug="side-cov",
        tokens=(TokenMeta(yes, "Yes"), TokenMeta(no, "No")),
        tick_size=args.tick_size,
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
    profile = load_named_profile(args.profile, config_dir=args.config_dir)
    rows = filter_rows_for_tokens(load_journal(journal), yes_token=yes, no_token=no)

    with tempfile.TemporaryDirectory(prefix="side_cov_") as td:
        root = Path(td)
        jpath = write_sliced_journal(rows, root / "j.jsonl")
        mpath = root / "m.jsonl"
        run_replay(jpath, meta, profile, mpath, fill_mode="optimistic")
        tok = Counter()
        for line in mpath.open():
            e = json.loads(line)
            if e.get("event") != "quote":
                continue
            tid = e.get("token_id")
            tok["YES" if tid == yes else "NO" if tid == no else "other"] += 1

    st = ReplayState(meta=meta, profile=profile, fill_mode="optimistic")
    st.metrics = MetricsLogger(Path(tempfile.mktemp(suffix=".jsonl")), enabled=True)
    trade_stats = Counter()
    for row in rows:
        ts = float(row.get("ts") or 0.0)
        if row.get("kind") == "last_trade_price":
            tp = parse_last_trade(row.get("data") or {})
            if tp and tp.asset_id in (yes, no):
                trade_stats["trades"] += 1
                same = [
                    o
                    for o in st.live.values()
                    if o.token_id == tp.asset_id and o.side is Side.BUY
                ]
                label = "YES" if tp.asset_id == yes else "NO"
                trade_stats[f"trades_{label}"] += 1
                if same:
                    trade_stats[f"trades_{label}_with_same_bid"] += 1
                else:
                    trade_stats[f"trades_{label}_without_same_bid"] += 1
        if apply_journal_event(st, row):
            _recompute(st, ts)

    report = {
        "token_pair": pair.as_dict(),
        "n_quote_yes": int(tok.get("YES", 0)),
        "n_quote_no": int(tok.get("NO", 0)),
        "quote_yes_frac": (
            tok.get("YES", 0) / max(1, tok.get("YES", 0) + tok.get("NO", 0))
        ),
        "trades": dict(trade_stats),
        "note": (
            "pair_ok=false or extreme YES-only quoting contaminates AS EV; "
            "fix tokens from catalog before quant_edge_eval"
        ),
    }
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"status=OK pair_ok={pair.pair_ok} mean_sum={pair.mean_sum} "
        f"quotes YES={tok.get('YES',0)} NO={tok.get('NO',0)} "
        f"trades={dict(trade_stats)} report={path}"
    )
    return 0 if pair.pair_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
