#!/usr/bin/env python3
"""Audit dependencies against uv.lock hashes and a committed baseline (T1-07).

Flags:
  - registry packages missing artifact hashes
  - git/url/path sources (except local polymaker)
  - direct pyproject deps not pinned with ==
  - version/hash drift vs deps/baseline.json
  - suspicious METADATA / entry_point hints in the active venv (optional)

Usage:
  uv run python scripts/deps_audit.py
  uv run python scripts/deps_audit.py --write-baseline
  uv run python scripts/deps_audit.py --fail-on-flags
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.deps_audit import audit_lock, write_baseline


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", default="uv.lock")
    ap.add_argument("--pyproject", default="pyproject.toml")
    ap.add_argument("--baseline", default="deps/baseline.json")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--fail-on-flags", action="store_true")
    ap.add_argument("--skip-site-packages", action="store_true")
    args = ap.parse_args()

    lock = Path(args.lock)
    pyproject = Path(args.pyproject)
    baseline = Path(args.baseline)

    if args.write_baseline:
        write_baseline(lock, baseline)
        print(json.dumps({"wrote_baseline": str(baseline), "n": len(json.loads(baseline.read_text()))}))
        return 0

    site = None
    if not args.skip_site_packages:
        # active uv venv site-packages
        candidates = list(Path(".venv").glob("lib/python*/site-packages")) if Path(".venv").exists() else []
        site = candidates[0] if candidates else None

    report = audit_lock(lock, pyproject, baseline_path=baseline if baseline.exists() else None,
                        site_packages=site)
    # summarize for humans on stderr
    flagged = [p for p in report.packages if p.flags]
    print(
        f"status={'OK' if report.ok else 'FLAGS'} packages={len(report.packages)} "
        f"flagged={len(flagged)} bumps={len(report.baseline_bumps)}",
        file=sys.stderr,
    )
    for p in flagged:
        print(f"  package {p.name}=={p.version} source={p.source} flags={p.flags}", file=sys.stderr)
    for b in report.baseline_bumps:
        print(f"  bump {b}", file=sys.stderr)
    for f in report.flags:
        print(f"  report_flag {f}", file=sys.stderr)

    # compact stdout for evidence / CI
    summary = {
        "ok": report.ok,
        "n_packages": len(report.packages),
        "n_flagged_packages": len(flagged),
        "flags": report.flags,
        "baseline_bumps": report.baseline_bumps,
        "flagged": [{"name": p.name, "version": p.version, "flags": p.flags} for p in flagged],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.fail_on_flags and not report.ok:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
