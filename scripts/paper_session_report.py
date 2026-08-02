#!/usr/bin/env python3
"""Final report extractor for the 5-hour livecfg paper session.

Reads logs/paper.jsonl (engine log) + journal/paper.jsonl (raw events) and
computes: total fills, realized WR, PnL, and governor state.
Usage: uv run python scripts/paper_session_report.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def load_logs(path: Path) -> list[dict]:
    events = []
    if not path.exists():
        return events
    with path.open() as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)
    return events


def main() -> int:
    log_path = Path("logs/paper.jsonl")
    events = load_logs(log_path)
    if not events:
        print("status=NO_LOG")
        return 1

    fills = [e for e in events if e.get("event") == "fill"]
    requotes = [e for e in events if e.get("event") == "requote"]
    resolved = [e for e in events if e.get("event") == "fill_labels_resolved"]
    retrains = [e for e in events if e.get("event") == "fill_model_retrained"]
    loaded = [e for e in events if e.get("event") == "fill_model_loaded"]

    # Total fill PnL (markout-based, same convention as analysis)
    base_size = 4.0
    tp_pct, sl_pct = 0.02, 0.01
    win_pnl = 0.0
    loss_pnl = 0.0
    n_wins = 0
    n_losses = 0

    for f in fills:
        # fill log has markout? Use resolved labels if present; else estimate
        markout = f.get("markout", None)
        if markout is None:
            continue
        if markout > 0:
            win_pnl += base_size * tp_pct
            n_wins += 1
        else:
            loss_pnl += base_size * sl_pct
            n_losses += 1

    # Realized WR from governor outcomes (fill_labels_resolved)
    wr_sources = []
    for r in resolved:
        wr = r.get("realized_wr")
        if wr is not None:
            wr_sources.append(wr)

    print("=" * 60)
    print("5-HOUR PAPER SESSION REPORT (livecfg)")
    print("=" * 60)
    print(f"Fills logged:        {len(fills)}")
    print(f"Requotes:            {len(requotes)}")
    print(f"Label resolutions:   {len(resolved)}")
    print(f"Model retrains:      {len(retrains)}")

    if loaded:
        last = loaded[-1]
        print(f"Model: deployable={last.get('deployable')} "
              f"auc={last.get('auc')} corr={last.get('corr')} "
              f"samples={last.get('samples')}")

    if retrains:
        last = retrains[-1]
        print(f"Last retrain: n={last.get('n_samples')} "
              f"fill_rate={last.get('fill_rate')} auc={last.get('auc')} "
              f"deployable={last.get('deployable')}")

    print(f"\nFill markouts present: {n_wins + n_losses}")
    if n_wins + n_losses > 0:
        wr = n_wins / (n_wins + n_losses)
        print(f"WR (markout>0):      {wr:.1%}")
        print(f"Win PnL:              +${win_pnl:.2f}")
        print(f"Loss PnL:             -${loss_pnl:.2f}")
        print(f"Net PnL:              ${win_pnl - loss_pnl:+.2f}")

    if wr_sources:
        print(f"\nGovernor realized WR (last {len(wr_sources)}): "
              f"{wr_sources[-1]:.1%}")

    # Governor mode distribution
    modes = Counter(r.get("mode", "?") for r in resolved)
    if modes:
        print(f"Governor modes:      {dict(modes)}")

    # Floors seen
    floors = [r.get("consensus_floor") for r in resolved if r.get("consensus_floor") is not None]
    if floors:
        print(f"Floors: min={min(floors):.2f} max={max(floors):.2f} last={floors[-1]:.2f}")

    print("\nNote: WR from markout>0 on logged fills; governor WR from resolved labels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
