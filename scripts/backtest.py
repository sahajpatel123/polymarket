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
    """Span of strategy activity on the journal timeline (quote/mark/fill).

    Excludes market_meta-only stamps so a stray wall-clock default cannot
    inflate daily_return_pct. Prefer quote timestamps when present.
    """
    import json as _json

    activity_ts: list[float] = []
    all_ts: list[float] = []
    if not metrics_path.exists():
        return 0.0
    with metrics_path.open() as fh:
        for line in fh:
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            t = obj.get("ts")
            if t is None:
                continue
            try:
                tf = float(t)
            except (TypeError, ValueError):
                continue
            all_ts.append(tf)
            ev = str(obj.get("event") or "")
            if ev in ("quote", "mark", "fill", "cancel"):
                activity_ts.append(tf)
    use = activity_ts if len(activity_ts) >= 2 else all_ts
    if len(use) < 2:
        return 0.0
    return max(0.0, (max(use) - min(use)) / 3600.0)


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
    # Scale sizes from bankroll. --bankroll flag wins over risk.bankroll_usdc.
    if args.bankroll > 0:
        bankroll = float(args.bankroll)
    else:
        bankroll = float(cfg.risk.bankroll_usdc) or 100.0
    if cfg.risk.bankroll_usdc <= 0 and args.bankroll > 0:
        from polymaker.config import RiskConfig

        risk = RiskConfig(bankroll_usdc=bankroll).resolve_from_bankroll()
        profile = risk.scale_profile_sizes(profile)
    elif cfg.risk.bankroll_usdc > 0:
        profile = cfg.risk.scale_profile_sizes(profile)
        # Don't override bankroll here — the --bankroll flag already won above.
        if args.bankroll <= 0:
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
    # Risk-parity allocation across journal markets (same path as engine discovery)
    from polymaker.catalog.scoring import score_market
    from polymaker.strategy.allocation import AllocationInputs, allocate_capital

    max_markets = max(1, int(getattr(cfg.engine, "auto_discovery_max_markets", 2) or 2))
    meta_cache: dict[str, MarketMeta] = {}
    alloc_rows: list[tuple[str, float, float]] = []
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
        sc = score_market(meta, bankroll_usdc=bankroll)
        risk = max(0.01, 1.0 / max(meta.liquidity_num / 10000.0, 0.1))
        # Expected return for allocation: prefer score when available
        # (already reward/AS-aware), else fall back to raw daily rate.
        # Floor at 1e-6 so zero-reward markets don't dominate the pool.
        exp_return = max(max(sc.score, float(meta.rewards_daily_rate or 0.0)), 1e-6)
        alloc_rows.append((cid, exp_return, risk))
    alloc = allocate_capital(AllocationInputs(
        markets=tuple(alloc_rows),
        total_capital_usdc=bankroll,
        max_concentration=float(cfg.risk.max_market_concentration_pct or 0.45),
        min_allocation=0.05,
    ))
    capital_by_cid: dict[str, float] = {a.condition_id: a.capital_usdc for a in alloc.allocations}
    if capital_by_cid:
        ordered = sorted(capital_by_cid, key=lambda c: -capital_by_cid[c])[:max_markets]
        cids = [c for c in ordered if c in meta_cache]
    else:
        cids = sorted(meta_cache, key=lambda c: -float(meta_cache[c].rewards_daily_rate or 0))[:max_markets]
    n_mkts = max(1, len(cids))
    print(f"funding {n_mkts} market(s) via allocate_capital: "
          f"{[(c[:16], round(capital_by_cid.get(c, bankroll / n_mkts), 1)) for c in cids]}")

    for cid in cids:
        capital_per_market = float(capital_by_cid.get(cid, bankroll / n_mkts))
        print(f"\n--- Backtesting {cid[:16]}... with profile '{args.profile}' "
              f"capital=${capital_per_market:.1f} ---")

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
        # MetricsLogger opens append-mode; wipe so A/B runs don't mix old quotes
        if metrics_path.exists():
            metrics_path.unlink()
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
        # Honest share: measured mean resting notional from metrics (not profile
        # layers post-hoc). Equal-share fallback when no resting samples.
        measured_notional = float(report.mean_resting_notional_usdc.get(cid, 0.0) or 0.0)
        if measured_notional <= 0:
            # equal competition among n markets (no free max_share bump)
            measured_notional = capital_per_market
            equal_share = True
        else:
            equal_share = False
        our_quote = min(measured_notional, capital_per_market)
        in_band_s = float(report.in_band_seconds.get(cid, 0.0) or 0.0)
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
        print(f"    reward_pool=${reward_pnl:.4f} in_band_s={in_band_s:.1f}")
        print(f"    resting_notional=${our_quote:.2f} equal_share_fallback={equal_share}")
        print(f"    our_share={est.our_reward_share:.4f} our_reward=${est.reward_our_usdc:.4f}")
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
                "in_band_seconds": round(in_band_s, 3),
                "mean_resting_notional_usdc": round(our_quote, 4),
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

    # Portfolio-level returns over the *journal* window (not wall-clock).
    portfolio_total = total_spread + total_reward_our + total_rebate
    b = max(bankroll, 1e-9)
    period_return = portfolio_total / b  # return over the observed window
    days = max(max_runtime_h, 1.0 / 60.0) / 24.0
    # Annualize to a 24h rate only as an extrapolation of this window.
    portfolio_daily = period_return / days if days > 0 else 0.0
    gap = max(0.0, 0.15 - portfolio_daily)

    # OOB / dust validation on this run's metrics (separate, explicit check)
    oob_n = dust_n = quote_n = fill_n = 0
    for cid in cids:
        mp = out_dir / f"metrics_{cid[:12]}.jsonl"
        if not mp.exists():
            continue
        with mp.open() as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = obj.get("event")
                if ev == "fill":
                    fill_n += 1
                if ev != "quote":
                    continue
                quote_n += 1
                try:
                    px = float(obj.get("price") or 0.0)
                except (TypeError, ValueError):
                    px = 0.0
                if px <= 0.001 + 1e-9:
                    dust_n += 1
                if not obj.get("in_reward_band"):
                    oob_n += 1

    summary = {
        "profile": args.profile,
        "bankroll_usdc": bankroll,
        "runtime_hours": round(max_runtime_h, 6),
        "spread_usdc": round(total_spread, 6),
        "reward_pool_usdc": round(total_reward_pool, 6),
        "reward_our_usdc": round(total_reward_our, 6),
        "rebate_est_usdc": round(total_rebate, 6),
        "total_est_usdc": round(portfolio_total, 6),
        "period_return_pct": round(period_return, 8),
        "daily_return_pct": round(portfolio_daily, 8),
        "gap_to_15pct": round(gap, 8),
        "target_band_hit": portfolio_daily >= 0.15,
        "n_fill": fill_n,
        "estimate_is_reward_only": fill_n == 0 and abs(total_spread) < 1e-9,
        "oob_check": {
            "quotes": quote_n,
            "dust_le_0.001": dust_n,
            "oob": oob_n,
            "ok": dust_n == 0 and oob_n == 0,
        },
        "results": all_results,
    }
    summary_path = out_dir / "backtest_summary.json"
    with summary_path.open("w") as fh:
        json.dump(summary, fh, indent=2, default=str)

    print("\n=== PORTFOLIO ===")
    print(f"total_est=${portfolio_total:.4f}")
    print(f"period_return_pct={period_return:.4%}  # over journal window only")
    print(f"daily_return_pct={portfolio_daily:.4%}  # period / (runtime_h/24) extrapolation")
    print(f"gap_to_15pct={gap:.4%} target_band_hit={portfolio_daily >= 0.15}")
    print(f"runtime_hours={max_runtime_h:.4f} (journal activity span, not wall-clock)")
    print(f"n_fill={fill_n} estimate_is_reward_only={summary['estimate_is_reward_only']}")
    if summary["estimate_is_reward_only"]:
        print("  NOTE: 0 fills — total_est is share-adjusted reward accrual only, not fill PnL")
    print(f"oob_check quotes={quote_n} dust_le_0.001={dust_n} oob={oob_n} "
          f"ok={summary['oob_check']['ok']}")
    print(f"Summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
