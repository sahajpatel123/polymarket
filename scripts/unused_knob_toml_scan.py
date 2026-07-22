#!/usr/bin/env python3
"""Flag unused StrategyProfile knobs that are still set in strategy TOML.

C-04 hygiene: operators often set ``exit_urgency_s`` / ``end_date_taper_days`` /
``event_sweep_levels`` expecting an effect — those fields are unused by the
live path today. This scan lists set-but-inert knobs without changing math.

Usage:
  uv run python scripts/unused_knob_toml_scan.py
  uv run python scripts/unused_knob_toml_scan.py --fail-on-set
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from polymaker.strategy.knob_audit import DEFAULT_ROOTS, audit_profile_knobs

DEFAULT_TOMLs = (
    Path("config/strategy.toml"),
    Path("livecfg/strategy.toml"),
)


def scan_toml(path: Path, unused: set[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    hits: list[dict[str, Any]] = []
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        # Allow flat tables for tests / ad-hoc fixtures.
        profiles = {k: v for k, v in data.items() if isinstance(v, dict)}
    for profile, body in profiles.items():
        if not isinstance(body, dict):
            continue
        for key, val in body.items():
            if key in unused:
                hits.append({
                    "path": str(path),
                    "profile": profile,
                    "knob": key,
                    "value": val,
                })
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--toml",
        action="append",
        default=None,
        help="strategy.toml path (repeatable). Default: config/ + livecfg/",
    )
    ap.add_argument(
        "--fail-on-set",
        action="store_true",
        help="Exit 1 if any unused knob is set in scanned TOML",
    )
    args = ap.parse_args()
    paths = [Path(p) for p in args.toml] if args.toml else list(DEFAULT_TOMLs)
    unused = set(audit_profile_knobs(DEFAULT_ROOTS).unused)
    hits: list[dict[str, Any]] = []
    for p in paths:
        hits.extend(scan_toml(p, unused))
    knobs = sorted({h["knob"] for h in hits})
    report = {
        "unused_fields": sorted(unused),
        "tomls": [str(p) for p in paths],
        "n_set_unused": len(hits),
        "set_unused_knobs": knobs,
        "hits": hits,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    print(
        f"status=OK n_set_unused={len(hits)} "
        f"knobs={','.join(knobs) or '(none)'} "
        f"profiles={len({(h['path'], h['profile']) for h in hits})}",
        file=sys.stderr,
    )
    if args.fail_on_set and hits:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
