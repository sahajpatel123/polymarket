#!/usr/bin/env python3
"""Print StrategyProfile knobs used vs unused by live strategy/engine code.

Usage:
  uv run python scripts/profile_knob_audit.py
  uv run python scripts/profile_knob_audit.py --fail-on-unused
"""

from __future__ import annotations

import argparse
import json
import sys

from polymaker.strategy.knob_audit import DEFAULT_ROOTS, audit_profile_knobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        action="append",
        default=None,
        help="Extra/override scan roots (repeatable). Default: strategy+engine+…",
    )
    ap.add_argument(
        "--fail-on-unused",
        action="store_true",
        help="Exit 1 if any StrategyProfile field is unused (CI optional)",
    )
    args = ap.parse_args()
    roots = args.root if args.root else DEFAULT_ROOTS
    rep = audit_profile_knobs(roots)
    print(json.dumps(rep.as_dict(), indent=2, sort_keys=True))
    print(
        f"status=OK n_used={rep.as_dict()['n_used']} "
        f"n_unused={rep.as_dict()['n_unused']} unused={','.join(rep.unused) or '(none)'}",
        file=sys.stderr,
    )
    if args.fail_on_unused and rep.unused:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
