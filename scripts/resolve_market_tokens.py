#!/usr/bin/env python3
"""Resolve YES/NO tokens + market meta from catalog by slug.

Usage:
  uv run python scripts/resolve_market_tokens.py \\
      --slug will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568 \\
      --db livecfg/state.db
"""

from __future__ import annotations

import argparse
import json
import sys

from polymaker.replay.market_resolve import meta_token_summary, resolve_market_by_slug
from polymaker.replay.token_pair_sanity import assess_token_pair


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--journal", default=None, help="Optional: validate pair on journal")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    try:
        meta = resolve_market_by_slug(args.slug, db_path=args.db)
    except (KeyError, ValueError) as e:
        print(f"status=ERROR reason={e}", file=sys.stderr)
        return 2

    out = meta_token_summary(meta)
    if args.journal:
        from pathlib import Path

        sanity = assess_token_pair(
            Path(args.journal), meta.yes.token_id, meta.no.token_id
        )
        out["token_pair"] = sanity.as_dict()
        if not sanity.pair_ok:
            print(
                f"status=ERROR pair_ok=false reason={sanity.reason} "
                f"yes={out['yes_token'][:16]}… no={out['no_token'][:16]}…",
                file=sys.stderr,
            )
            if args.report:
                Path(args.report).write_text(json.dumps(out, indent=2) + "\n")
            return 3

    if args.report:
        from pathlib import Path

        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    print(
        f"status=OK slug={out['slug']} cid={out['condition_id']} "
        f"yes={out['yes_token']} no={out['no_token']} "
        f"tick={out['tick_size']} rewards_max_spread={out['rewards_max_spread']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
