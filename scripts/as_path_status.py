#!/usr/bin/env python3
"""Print AS-path readiness for one or more market slugs.

Usage:
  uv run python scripts/as_path_status.py \\
      --journal livecfg/journal/paper.jsonl.pre12h… \\
      --slugs will-gavin-newsom-…,will-jd-vance-… \\
      --db livecfg/state.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.replay.as_path_status import assess_as_path_from_journal
from polymaker.replay.market_resolve import resolve_market_by_slug


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True)
    ap.add_argument(
        "--slugs",
        default=(
            "will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568,"
            "will-jd-vance-win-the-2028-republican-presidential-nomination"
        ),
    )
    ap.add_argument("--db", default="livecfg/state.db")
    ap.add_argument("--report", default="logs/as_path_status/report.json")
    args = ap.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"status=ERROR reason=missing_journal path={journal}", file=sys.stderr)
        return 2

    rows = []
    any_ready = False
    for slug in [s.strip() for s in args.slugs.split(",") if s.strip()]:
        meta = resolve_market_by_slug(slug, db_path=args.db)
        st = assess_as_path_from_journal(journal, meta)
        d = st.as_dict()
        rows.append(d)
        any_ready = any_ready or st.ready
        print(
            f"slug={slug} ready={st.ready} through={st.n_through} "
            f"at_touch={st.n_at_touch} blockers={','.join(st.blockers)}"
        )

    report = {
        "journal": str(journal),
        "any_ready": any_ready,
        "markets": rows,
        "policy": (
            "Do not soften conservative equal-price skip to force a finding. "
            "Unblock via denser through-price tape or explicit Tier-2 policy PR."
        ),
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"status=OK any_ready={any_ready} report={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
