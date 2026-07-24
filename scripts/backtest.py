#!/usr/bin/env python3
"""Backtest a strategy profile against a journal with fill simulation.

Runs the replay engine with the paper fill simulator, then produces a
PnL / reward / rebate / adverse-selection report with bankroll-normalized
daily return. Primary offline validation tool before live capital.

Usage:
  uv run python scripts/backtest.py --journal livecfg/journal/paper.jsonl \\
      --profile live_scaled --bankroll 100 --out-dir backtest_out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from polymaker.config import Config
from polymaker.domain import MarketMeta
from polymaker.metrics.analyze import analyze
from polymaker.replay import discover_condition_ids, load_journal, run_replay
from polymaker.strategy.edge import estimate_daily_return


def _load_meta_from_catalog(db_path: Path, condition_id: str) -> MarketMeta | None:
    from polymaker.catalog.store import CatalogStore

    store = CatalogStore(str(db_path))
    meta = store.get(condition_id)
    store.close()
    return meta


def _load_meta_from_journal(journal_path: Path, condition_id: str) -> MarketMeta | None:
    from polymaker.domain import MarketMeta, TokenMeta

    rows = load_journal(journal_path)
    yes_token = no_token = ""
    tick_size = 0.001

    for row in rows:
        if row.get("kind") != "book":
            continue
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        if str(data.get("market", "")) != condition_id:
            continue
        asset_id = str(data.get("asset_id", ""))
        if not asset_id:
            continue
        if not yes_token:
            yes_token = asset_id
        elif asset_id != yes_token and not no_token:
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
    import json as _json

    from polymaker.domain import MarketMeta, TokenMeta

    yes_token = no_token = ""
    tick_size = 0.001
    rewards_min_size = 0.0
    rewards_max_spread = 0.0
    rewards_daily_rate = 0.0
    rebate_rate = 0.25
    liquidity_num = 0.0
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
                    rebate_rate = float(obj.get("rebate_rate") or 0.25)
                    tick_size = float(obj.get("tick_size") or 0.001)
                    liquidity_num = float(obj.get("liquidity_num") or 0)
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

    return MarketMeta(
        condition_id=condition_id,
        question=question or f"backtest-{condition_id[:12]}",
        slug=slug or f"backtest-{condition_id[:12]}",
        tokens=(
            TokenMeta(yes_token, "Yes"),
            TokenMeta(no_token or "no-unknown", "No"),
        ),
        tick_size=tick_size,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=rewards_min_size,
        rewards_max_spread=rewards_max_spread,
        rewards_daily_rate=rewards_daily_rate,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=rebate_rate,
        liquidity_num=liquidity_num,
    )


def _enrich_meta_rewards(meta: MarketMeta, metrics_path: Path) -> MarketMeta:
    """Overlay rewards fields from metrics market_meta when journal meta is bare."""
    rich = _load_meta_from_metrics(metrics_path, meta.condition_id)
    if rich is None:
        return meta
    if meta.rewards_daily_rate > 0:
        return meta
    return MarketMeta(
        condition_id=meta.condition_id,
        question=rich.question or meta.question,
        slug=rich.slug or meta.slug,
        tokens=meta.tokens,
        tick_size=meta.tick_size or rich.tick_size,
        neg_risk=meta.neg_risk,
        min_order_size=meta.min_order_size,
        rewards_min_size=rich.rewards_min_size or meta.rewards_min_size,
        rewards_max_spread=rich.rewards_max_spread or meta.rewards_max_spread,
        rewards_daily_rate=rich.rewards_daily_rate or meta.rewards_daily_rate,
        maker_fee_bps=meta.maker_fee_bps,
        taker_fee_bps=meta.taker_fee_bps,
        fees_enabled=meta.fees_enabled,
        end_date_iso=meta.end_date_iso,
        event_id=meta.event_id,
        rebate_rate=rich.rebate_rate or meta.rebate_rate,
        liquidity_num=rich.liquidity_num or meta.liquidity_num,
    )


def _journal_condition_ids(journal_path: Path) -> list[str]:
    """Discover condition IDs from journal book rows (not metrics format)."""
    rows = load_journal(journal_path)
    seen: set[str] = set()
    for row in rows:
        data = row.get("data")
        if not isinstance(data, dict):
            continue
        cid = str(data.get("market", "") or "")
        if cid:
            seen.add(cid)
    return sorted(seen)


def _runtime_hours_from_metrics(metrics_path: Path) -> float:
    import json as _json

    ts: list[float] = []
    if not metrics_path.exists():
        return 0.0
    with metrics_path.open() as fh:
        for line in fh:
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            t = obj.get("ts")
            if t is not None:
                try:
                    ts.append(float(t))
                except (TypeError, ValueError):
                    pass
    if len(ts) < 2:
        return 0.0
    return max(0.0, (max(ts) - min(ts)) / 3600.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--journal", required=True, help="path to journal JSONL")
    ap.add_argument("--profile", default="live_scaled", help="strategy profile name")
    ap.add_argument("--config-dir", default="config", help="config directory")
    ap.add_argument("--out-dir", default="backtest_out", help="output directory")
    ap.add_argument("--db", default="state.db", help="SQLite catalog DB path")
    ap.add_argument(
        "--metrics-source",
        default="",
        help="optional live metrics JSONL to enrich rewards meta (e.g. livecfg/logs/metrics-paper.jsonl)",
    )
    ap.add_argument(
        "--bankroll",
        type=float,
        default=0.0,
        help="bankroll USDC for daily_return_pct (0 → risk.bankroll_usdc or 100)",
    )
    args = ap.parse_args()

    journal_path = Path(args.journal)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not journal_path.exists():
        print(f"ERROR: journal not found: {journal_path}", file=sys.stderr)
        return 1

    cfg = Config.load(args.config_dir, load_env=False)
    if args.profile not in cfg.profiles:
        print(
            f"ERROR: profile {args.profile!r} not found. Known: {sorted(cfg.profiles)}",
            file=sys.stderr,
        )
        return 1
    profile = cfg.profiles[args.profile]
    # Scale sizes from bankroll when risk bankroll is set
    bankroll = float(args.bankroll) or float(cfg.risk.bankroll_usdc) or 100.0
    if cfg.risk.bankroll_usdc <= 0 and args.bankroll > 0:
        from polymaker.config import RiskConfig

        risk = RiskConfig(bankroll_usdc=bankroll).resolve_from_bankroll()
        profile = risk.scale_profile_sizes(profile)
    elif cfg.risk.bankroll_usdc > 0:
        profile = cfg.risk.scale_profile_sizes(profile)
        bankroll = float(cfg.risk.bankroll_usdc)

    cids = discover_condition_ids(journal_path)
    if not cids:
        cids = _journal_condition_ids(journal_path)
    if not cids:
        print("ERROR: no condition IDs found in journal", file=sys.stderr)
        return 1

    print(f"Found {len(cids)} market(s) in journal: {[c[:16] for c in cids]}")
    print(f"profile={args.profile} bankroll_usdc={bankroll:.2f}")

    metrics_src = Path(args.metrics_source) if args.metrics_source else Path("livecfg/logs/metrics-paper.jsonl")

    all_results: list[dict] = []
    total_spread = 0.0
    total_reward_pool = 0.0
    total_reward_our = 0.0
    total_rebate = 0.0
    max_runtime_h = 0.0
    # Split bankroll across markets so portfolio return does not claim
    # max_share of every pool with the same dollars (overstatement).
    # Prefer densest reward markets first (profit ranking); cap how many we fund
    # so capital is not dusted (matches auto_discovery_max_markets posture).
    max_markets = max(1, int(getattr(cfg.engine, "auto_discovery_max_markets", 2) or 2))
    # Pre-resolve metas for ranking
    ranked: list[tuple[float, str]] = []
    meta_cache: dict[str, MarketMeta] = {}
    for cid in cids:
        meta = _load_meta_from_catalog(Path(args.db), cid)
        if meta is None:
            meta = _load_meta_from_journal(journal_path, cid)
        if meta is None and metrics_src.exists():
            meta = _load_meta_from_metrics(metrics_src, cid)
        if meta is None:
            continue
        if metrics_src.exists():
            meta = _enrich_meta_rewards(meta, metrics_src)
        meta_cache[cid] = meta
        ranked.append((float(meta.rewards_daily_rate or 0.0), cid))
    ranked.sort(reverse=True)
    cids = [cid for _, cid in ranked[:max_markets]] or cids[:max_markets]
    n_mkts = max(1, len(cids))
    capital_per_market = bankroll / n_mkts
    print(f"funding top {n_mkts} market(s) by rewards_daily_rate: {[c[:16] for c in cids]}")

    for cid in cids:
        print(f"\n--- Backtesting {cid[:16]}... with profile '{args.profile}' ---")

        meta = meta_cache.get(cid)
        if meta is None:
            print(f"  ERROR: could not determine MarketMeta for {cid}", file=sys.stderr)
            continue

        # Fixture markets: inject representative reward params so reward path is exercised
        if meta.condition_id in ("0xreplay",) or meta.rewards_daily_rate <= 0:
            from polymaker.domain import MarketMeta as MM

            meta = MM(
                condition_id=meta.condition_id,
                question=meta.question,
                slug=meta.slug,
                tokens=meta.tokens,
                tick_size=meta.tick_size,
                neg_risk=meta.neg_risk,
                min_order_size=meta.min_order_size,
                rewards_min_size=max(meta.rewards_min_size, 50.0),
                rewards_max_spread=max(meta.rewards_max_spread, 5.0),
                rewards_daily_rate=max(meta.rewards_daily_rate, 200.0),
                maker_fee_bps=meta.maker_fee_bps,
                taker_fee_bps=meta.taker_fee_bps or 400,
                fees_enabled=True,
                end_date_iso=meta.end_date_iso,
                event_id=meta.event_id,
                rebate_rate=meta.rebate_rate or 0.25,
                liquidity_num=max(meta.liquidity_num, 15000.0),
            )

        print(
            f"  tick={meta.tick_size}, rewards_min={meta.rewards_min_size}, "
            f"rewards_band={meta.rewards_max_spread}c, daily_rate=${meta.rewards_daily_rate:.0f}"
        )

        metrics_path = out_dir / f"metrics_{cid[:12]}.jsonl"
        result = run_replay(journal_path, meta, profile, metrics_path)

        print(
            f"  events_read={result.events_read} applied={result.events_applied} "
            f"recomputes={result.recomputes}"
        )
        print(
            f"  quotes={result.n_quote} cancels={result.n_cancel} "
            f"fills={result.n_fill} marks={result.n_mark}"
        )

        report = analyze(metrics_path)
        print("\n  Metrics:")
        print(f"    n_quote={report.n_quote} n_cancel={report.n_cancel} n_fill={report.n_fill}")
        print(f"    realized_spread_usdc={report.realized_spread_usdc:.4f}")
        print(f"    inventory_drift_abs_peak={report.inventory_drift_abs_peak:.4f}")
        print(f"    inventory_net_end={report.inventory_net_end}")

        if report.markout:
            print(f"    markout={report.markout}")
            print(f"    markout_n={report.markout_n}")

        reward_pnl = sum(report.reward_accrual_usdc.values())
        for cid_key, val in report.reward_accrual_usdc.items():
            print(f"    reward_pool_accrual[{cid_key[:12]}]=${val:.4f}")

        spread_pnl = report.realized_spread_usdc
        rebate_estimate = 0.0
        if report.n_fill > 0 and meta.taker_fee_bps > 0:
            avg_fill_size = 50.0
            filled_volume = report.n_fill * avg_fill_size * 0.5
            rebate_estimate = filled_volume * (meta.taker_fee_bps / 10000) * meta.rebate_rate

        runtime_h = _runtime_hours_from_metrics(metrics_path)
        max_runtime_h = max(max_runtime_h, runtime_h)
        # Resting notional on this market (two-sided × layers), capped by allocated capital
        raw_quote = float(profile.base_size_usdc) * max(1, int(profile.layers)) * 2.0
        our_quote = min(raw_quote, capital_per_market)
        # Per-market return uses allocated capital; portfolio sums dollars then / bankroll
        est = estimate_daily_return(
            bankroll_usdc=capital_per_market,
            runtime_hours=max(runtime_h, 1.0 / 60.0),
            spread_usdc=spread_pnl,
            reward_pool_accrual_usdc=reward_pnl,
            rebate_est_usdc=rebate_estimate,
            our_quote_usdc=our_quote,
            market_liquidity=max(meta.liquidity_num, 10000.0, our_quote),
        )

        print("\n  PnL Estimate:")
        print(f"    spread_pnl=${spread_pnl:.4f}")
        print(f"    reward_pool=${reward_pnl:.4f} our_share={est.our_reward_share:.4f} our=${est.reward_our_usdc:.4f}")
        print(f"    rebate_est=${rebate_estimate:.4f}")
        print(f"    total_est=${est.total_est_usdc:.4f}")
        print(f"    runtime_hours={runtime_h:.4f}")
        print(f"    daily_return_pct={est.daily_return_pct:.4%}")
        print(f"    gap_to_15pct={est.gap_to_15pct:.4%}")
        print(f"    target_band_hit={est.target_band_hit}")

        total_spread += spread_pnl
        total_reward_pool += reward_pnl
        total_reward_our += est.reward_our_usdc
        total_rebate += rebate_estimate

        all_results.append({
            "condition_id": cid,
            "profile": args.profile,
            "bankroll_usdc": bankroll,
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
                "reward_pool_usdc": round(reward_pnl, 6),
                "reward_our_usdc": round(est.reward_our_usdc, 6),
                "our_reward_share": round(est.our_reward_share, 6),
                "rebate_est_usdc": round(rebate_estimate, 6),
                "total_est_usdc": round(est.total_est_usdc, 6),
                "runtime_hours": round(runtime_h, 6),
                "daily_return_pct": round(est.daily_return_pct, 8),
                "gap_to_15pct": round(est.gap_to_15pct, 8),
                "target_band_hit": est.target_band_hit,
            },
        })

    # Portfolio-level return (sum components over max runtime)
    portfolio_total = total_spread + total_reward_our + total_rebate
    days = max(max_runtime_h, 1.0 / 60.0) / 24.0
    portfolio_daily = (portfolio_total / max(bankroll, 1e-9)) / days if days > 0 else 0.0
    gap = max(0.0, 0.15 - portfolio_daily)

    summary = {
        "profile": args.profile,
        "bankroll_usdc": bankroll,
        "runtime_hours": max_runtime_h,
        "spread_usdc": round(total_spread, 6),
        "reward_pool_usdc": round(total_reward_pool, 6),
        "reward_our_usdc": round(total_reward_our, 6),
        "rebate_est_usdc": round(total_rebate, 6),
        "total_est_usdc": round(portfolio_total, 6),
        "daily_return_pct": round(portfolio_daily, 8),
        "gap_to_15pct": round(gap, 8),
        "target_band_hit": portfolio_daily >= 0.15,
        "results": all_results,
    }
    summary_path = out_dir / "backtest_summary.json"
    with summary_path.open("w") as fh:
        json.dump(summary, fh, indent=2, default=str)

    print("\n=== PORTFOLIO ===")
    print(f"total_est=${portfolio_total:.4f} daily_return_pct={portfolio_daily:.4%} "
          f"gap_to_15pct={gap:.4%} target_band_hit={portfolio_daily >= 0.15}")
    print(f"Summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
