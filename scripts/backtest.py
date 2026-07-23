#!/usr/bin/env python3
"""Backtest a strategy profile against a journal with fill simulation.

Runs the replay engine with the paper fill simulator, then produces a
PnL / reward / rebate / adverse-selection report. This is the primary
offline validation tool before going live with real capital.

Usage:
  uv run python scripts/backtest.py --journal journal/paper.jsonl \\
      --profile political-longdated --out-dir backtest_out/

The journal must contain book snapshots, price changes, and trade prints
(the same format the engine journals to journal/).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.config import Config
from polymaker.domain import MarketMeta
from polymaker.metrics.analyze import analyze
from polymaker.replay import discover_condition_ids, run_replay


def _load_meta_from_catalog(db_path: Path, condition_id: str) -> MarketMeta | None:
    """Load MarketMeta from the SQLite catalog by condition_id."""
    from polymaker.catalog.store import CatalogStore

    store = CatalogStore(str(db_path))
    meta = store.get(condition_id)
    store.close()
    return meta


def _load_meta_from_journal(journal_path: Path, condition_id: str) -> MarketMeta | None:
    """Infer MarketMeta from journal book rows (tick size, token ids)."""
    from polymaker.domain import MarketMeta, TokenMeta
    from polymaker.replay import load_journal

    rows = load_journal(journal_path)
    yes_token = no_token = ""
    tick_size = 0.001

    for row in rows:
        if row.get("kind") != "book" or not isinstance(row.get("data"), dict):
            continue
        data = row["data"]
        if str(data.get("market", "")) != condition_id:
            continue
        asset_id = str(data.get("asset_id", ""))
        if not asset_id:
            continue
        if not yes_token:
            yes_token = asset_id
        elif not no_token:
            no_token = asset_id
        ts = data.get("tick_size")
        if ts:
            tick_size = float(ts)

    if not yes_token or not no_token:
        return None

    return MarketMeta(
        condition_id=condition_id,
        question=f"backtest-{condition_id[:12]}",
        slug=f"backtest-{condition_id[:12]}",
        tokens=(
            TokenMeta(yes_token, "Yes"),
            TokenMeta(no_token, "No"),
        ),
        tick_size=tick_size,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=0.0,
        rewards_max_spread=0.0,
        rewards_daily_rate=0.0,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
    )


def _load_meta_from_metrics(metrics_path: Path, condition_id: str) -> MarketMeta | None:
    """Infer MarketMeta from metrics log (token ids + tick size + rewards)."""
    import json as _json

    yes_token = no_token = ""
    tick_size = 0.001
    rewards_min_size = 0.0
    rewards_max_spread = 0.0
    rewards_daily_rate = 0.0
    rebate_rate = 0.0
    question = ""
    slug = ""

    means: dict[str, list[float]] = {}
    if metrics_path.exists():
        with metrics_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if obj.get("event") == "quote" and str(obj.get("condition_id")) == condition_id:
                    tid = str(obj.get("token_id") or "")
                    try:
                        px = float(obj.get("price"))
                    except (TypeError, ValueError):
                        continue
                    if tid:
                        means.setdefault(tid, []).append(px)
                elif obj.get("event") == "market_meta" and str(obj.get("condition_id")) == condition_id:
                    rewards_min_size = float(obj.get("rewards_min_size") or 0)
                    rewards_max_spread = float(obj.get("rewards_max_spread") or 0)
                    rewards_daily_rate = float(obj.get("rewards_daily_rate") or 0)
                    rebate_rate = float(obj.get("rebate_rate") or 0)
                    tick_size = float(obj.get("tick_size") or 0.001)
                    question = str(obj.get("question") or "")
                    slug = str(obj.get("slug") or "")

    if len(means) >= 2:
        ranked = sorted(
            ((tid, sum(xs) / len(xs)) for tid, xs in means.items()),
            key=lambda kv: kv[1],
        )
        yes_token = ranked[0][0]
        no_token = ranked[1][0]

    if not yes_token:
        return None

    from polymaker.domain import MarketMeta, TokenMeta

    return MarketMeta(
        condition_id=condition_id,
        question=question or f"backtest-{condition_id[:12]}",
        slug=slug or f"backtest-{condition_id[:12]}",
        tokens=(
            TokenMeta(yes_token, "Yes"),
            TokenMeta(no_token, "No"),
        ),
        tick_size=tick_size,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=rewards_min_size,
        rewards_max_spread=rewards_max_spread,
        rewards_daily_rate=rewards_daily_rate,
        maker_fee_bps=0,
        taker_fee_bps=400,  # V2 default ~4%
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=rebate_rate,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True, help="path to journal JSONL")
    ap.add_argument("--profile", default="political-longdated", help="strategy profile name")
    ap.add_argument("--config-dir", default="config", help="config directory")
    ap.add_argument("--out-dir", default="backtest_out", help="output directory")
    ap.add_argument("--db", default="state.db", help="SQLite catalog DB path (for MarketMeta)")
    args = ap.parse_args()

    journal_path = Path(args.journal)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not journal_path.exists():
        print(f"ERROR: journal not found: {journal_path}", file=sys.stderr)
        return 1

    # Load strategy profile
    cfg = Config.load(args.config_dir, load_env=False)
    if args.profile not in cfg.profiles:
        print(f"ERROR: profile {args.profile!r} not found. Known: {sorted(cfg.profiles)}", file=sys.stderr)
        return 1
    profile = cfg.profiles[args.profile]

    # Discover condition IDs from the journal
    cids = discover_condition_ids(journal_path)
    if not cids:
        # Try extracting ALL market IDs from journal rows
        from polymaker.replay import load_journal

        rows = load_journal(journal_path)
        seen: set[str] = set()
        for row in rows:
            if isinstance(row.get("data"), dict):
                cid = str(row["data"].get("market", ""))
                if cid and cid not in ("0xreplay", ""):
                    seen.add(cid)
        cids = sorted(seen)
        if not cids:
            print("ERROR: no condition IDs found in journal", file=sys.stderr)
            return 1

    print(f"Found {len(cids)} market(s) in journal: {cids}")

    all_results: list[dict] = []
    for cid in cids:
        print(f"\n--- Backtesting {cid[:16]}... with profile '{args.profile}' ---")

        # Try loading MarketMeta from catalog first, then from metrics, then from journal
        meta = _load_meta_from_catalog(Path(args.db), cid)
        if meta is None:
            print("  MarketMeta not in catalog, inferring from journal...")
            meta = _load_meta_from_journal(journal_path, cid)
        if meta is None:
            print(f"  ERROR: could not determine MarketMeta for {cid}", file=sys.stderr)
            continue

        print(f"  tick={meta.tick_size}, rewards_min={meta.rewards_min_size}, "
              f"rewards_band={meta.rewards_max_spread}c, daily_rate=${meta.rewards_daily_rate:.0f}")

        metrics_path = out_dir / f"metrics_{cid[:12]}.jsonl"
        result = run_replay(journal_path, meta, profile, metrics_path)

        print(f"  events_read={result.events_read} applied={result.events_applied} "
              f"recomputes={result.recomputes}")
        print(f"  quotes={result.n_quote} cancels={result.n_cancel} fills={result.n_fill} marks={result.n_mark}")

        # Analyze metrics
        report = analyze(metrics_path)
        print("\n  Metrics:")
        print(f"    n_quote={report.n_quote} n_cancel={report.n_cancel} n_fill={report.n_fill}")
        print(f"    realized_spread_usdc={report.realized_spread_usdc:.4f}")
        print(f"    inventory_drift_abs_peak={report.inventory_drift_abs_peak:.4f}")
        print(f"    inventory_net_end={report.inventory_net_end}")

        if report.markout:
            print(f"    markout={report.markout}")
            print(f"    markout_n={report.markout_n}")

        if report.reward_accrual_usdc:
            for cid_key, val in report.reward_accrual_usdc.items():
                print(f"    reward_accrual[{cid_key[:12]}]=${val:.4f}")

        if report.rebate_pool_daily_usdc:
            for cid_key, val in report.rebate_pool_daily_usdc.items():
                print(f"    rebate_pool_daily[{cid_key[:12]}]=${val:.4f}")

        # Compute summary PnL estimate
        spread_pnl = report.realized_spread_usdc
        reward_pnl = sum(report.reward_accrual_usdc.values())
        # Rebate estimate: 25% of taker fees on filled volume (crude)
        # This is a lower bound; actual rebates depend on volume share
        rebate_estimate = 0.0
        if report.n_fill > 0 and meta.taker_fee_bps > 0:
            # Approximate filled volume from fill count * avg size
            # (crude, but gives a ballpark)
            avg_fill_size = 50.0  # typical
            filled_volume = report.n_fill * avg_fill_size * 0.01  # price ~0.01
            rebate_estimate = filled_volume * (meta.taker_fee_bps / 10000) * meta.rebate_rate

        total_est = spread_pnl + reward_pnl + rebate_estimate
        print("\n  PnL Estimate:")
        print(f"    spread_pnl=${spread_pnl:.4f}")
        print(f"    reward_pnl=${reward_pnl:.4f}")
        print(f"    rebate_est=${rebate_estimate:.4f}")
        print(f"    total_est=${total_est:.4f}")

        all_results.append({
            "condition_id": cid,
            "profile": args.profile,
            "replay": {
                "events_read": result.events_read,
                "events_applied": result.events_applied,
                "recomputes": result.recomputes,
                "n_quote": result.n_quote,
                "n_cancel": result.n_cancel,
                "n_fill": result.n_fill,
                "n_mark": result.n_mark,
            },
            "metrics": report.as_dict(),
            "pnl_estimate": {
                "spread_usdc": round(spread_pnl, 6),
                "reward_usdc": round(reward_pnl, 6),
                "rebate_est_usdc": round(rebate_estimate, 6),
                "total_est_usdc": round(total_est, 6),
            },
        })

    # Write summary
    summary_path = out_dir / "backtest_summary.json"
    with summary_path.open("w") as fh:
        json.dump({"results": all_results}, fh, indent=2, default=str)
    print(f"\nSummary written to {summary_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
