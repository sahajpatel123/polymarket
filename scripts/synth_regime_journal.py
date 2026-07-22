#!/usr/bin/env python3
"""Write a quiet→jump→recovery synthetic journal for offline strategy eval.

Usage:
  uv run python scripts/synth_regime_journal.py --out fixtures/regime_jump.jsonl
  uv run python scripts/synth_regime_journal.py --dense --out fixtures/regime_dense.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.synth import write_regime_journal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="fixtures/regime_jump.jsonl")
    ap.add_argument("--quiet-steps", type=int, default=8)
    ap.add_argument("--jump-ticks", type=int, default=10)
    ap.add_argument("--recovery-steps", type=int, default=6)
    ap.add_argument("--cycles", type=int, default=1,
                    help="Repeat quiet→jump→recovery this many times")
    ap.add_argument(
        "--dense",
        action="store_true",
        help="Shorthand: 20 quiet / 12 recovery / 8 cycles (non-thin OOS)",
    )
    ap.add_argument("--tick-size", type=float, default=0.01)
    args = ap.parse_args()
    quiet = args.quiet_steps
    recovery = args.recovery_steps
    cycles = args.cycles
    out = args.out
    if args.dense:
        quiet = max(quiet, 20)
        recovery = max(recovery, 12)
        cycles = max(cycles, 8)
        if args.out == "fixtures/regime_jump.jsonl":
            out = "fixtures/regime_dense.jsonl"
    info = write_regime_journal(
        Path(out),
        quiet_steps=quiet,
        jump_ticks=args.jump_ticks,
        recovery_steps=recovery,
        cycles=cycles,
        tick=args.tick_size,
    )
    info["cycles"] = cycles
    print(json.dumps(info, indent=2, sort_keys=True))
    print(
        f"status=OK n_events={info['n_events']} cycles={cycles} path={info['path']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
