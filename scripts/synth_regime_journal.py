#!/usr/bin/env python3
"""Write a quiet→jump→recovery synthetic journal for offline strategy eval.

Usage:
  uv run python scripts/synth_regime_journal.py --out fixtures/regime_jump.jsonl
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
    ap.add_argument("--tick-size", type=float, default=0.01)
    args = ap.parse_args()
    info = write_regime_journal(
        Path(args.out),
        quiet_steps=args.quiet_steps,
        jump_ticks=args.jump_ticks,
        recovery_steps=args.recovery_steps,
        tick=args.tick_size,
    )
    print(json.dumps(info, indent=2, sort_keys=True))
    print(f"status=OK n_events={info['n_events']} path={info['path']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
