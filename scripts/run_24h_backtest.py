#!/usr/bin/env python3
"""Run a 24-hour paper backtest with $30 starting capital.

This script creates a realistic 24-hour synthetic journal and runs it
through the replay backtester with fill simulation enabled.

Features:
- Creates synthetic 24-hour market data with realistic price action
- Runs full strategy with quoting, reconciliation, and fill simulation
- Tracks PnL, rewards, adverse selection
- Generates comprehensive report
- Supports custom bankroll, profile, and parameters

Usage:
    # Default: 24h backtest with $30
    uv run python scripts/run_24h_backtest.py
    
    # Custom bankroll
    uv run python scripts/run_24h_backtest.py --bankroll 50
    
    # Custom profile
    uv run python scripts/run_24h_backtest.py --profile live-tiny
    
    # Output directory
    uv run python scripts/run_24h_backtest.py --out-dir my_backtest
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polymaker.benchmark import (
    BenchmarkStatus,
    ValidityConfig,
    check_capital_feasibility,
    evaluate_benchmark,
)
from polymaker.config import Config, RiskConfig, StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.metrics import MetricsLogger
from polymaker.metrics.analyze import analyze, MetricsReport
from polymaker.replay import run_replay, load_journal, ReplayResult


@dataclass
class SyntheticMarketParams:
    """Parameters for synthetic market generation."""
    
    # Market structure
    yes_token: str = "0x0000000000000000000000000000000000000001"
    no_token: str = "0x0000000000000000000000000000000000000002"
    market_id: str = "0x24h_backtest_market"
    condition_id: str = "0x24h_backtest"
    tick_size: float = 0.001
    
    # Initial price
    initial_yes_price: float = 0.45
    initial_no_price: float = 0.55
    
    # Price behavior
    volatility_sigma: float = 0.01  # Daily vol
    mean_reversion_strength: float = 0.5
    
    # Market activity
    trades_per_hour: int = 60  # ~1 trade per minute
    book_updates_per_hour: int = 120
    
    # Book depth
    min_book_depth: float = 1000.0
    max_book_depth: float = 10000.0
    
    # Trade sizes
    min_trade_size: float = 10.0
    max_trade_size: float = 500.0
    
    # Duration
    duration_hours: float = 24.0


@dataclass
class SyntheticTrade:
    """A synthetic trade event."""
    ts: float
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    token: str = ""


@dataclass
class SyntheticBookUpdate:
    """A synthetic book update."""
    ts: float
    asset_id: str
    bids: list[tuple[float, float]]  # (price, size)
    asks: list[tuple[float, float]]  # (price, size)


class SyntheticMarketGenerator:
    """Generates realistic synthetic market data."""
    
    def __init__(self, params: SyntheticMarketParams | None = None):
        self.params = params or SyntheticMarketParams()
        self.random = random.Random(42)  # Reproducible
        
        # Current state
        self.current_yes_price = self.params.initial_yes_price
        self.current_no_price = self.params.initial_no_price
        self.current_yes_bid = self.params.initial_yes_price - 0.01
        self.current_yes_ask = self.params.initial_yes_price + 0.01
        self.current_no_bid = self.params.initial_no_price - 0.01
        self.current_no_ask = self.params.initial_no_price + 0.01
        self.target_yes_price = self.params.initial_yes_price
        self.target_no_price = self.params.initial_no_price
        
    def set_seed(self, seed: int) -> None:
        """Set random seed for reproducibility."""
        self.random = random.Random(seed)
    
    def generate_price_path(self, n_steps: int, duration_hours: float) -> list[float]:
        """Generate a mean-reverting price path."""
        dt = duration_hours / n_steps  # hours per step
        prices = [self.params.initial_yes_price]
        
        for _ in range(n_steps - 1):
            # Mean reversion
            drift = self.params.mean_reversion_strength * (self.params.initial_yes_price - prices[-1]) * dt
            # Random walk
            shock = self.params.volatility_sigma * (dt ** 0.5) * self.random.gauss(0, 1)
            new_price = prices[-1] + drift + shock
            # Clamp to valid range
            new_price = max(0.001, min(0.999, new_price))
            prices.append(new_price)
        
        return prices
    
    def generate_trades(self, n_trades: int, start_ts: float, end_ts: float) -> list[SyntheticTrade]:
        """Generate synthetic trade events for both YES and NO tokens."""
        trades = []
        ts_step = (end_ts - start_ts) / n_trades
        
        for i in range(n_trades):
            ts = start_ts + i * ts_step + self.random.uniform(-0.5 * ts_step, 0.5 * ts_step)
            ts = max(start_ts, min(end_ts, ts))
            
            # Alternate between YES and NO tokens
            token = self.params.yes_token if i % 2 == 0 else self.params.no_token
            
            # Price with some random walk
            price_change = self.random.uniform(-0.005, 0.005)
            if token == self.params.yes_token:
                price = self.current_yes_price + price_change
                price = max(0.001, min(0.999, price))
                self.current_yes_price = price
            else:
                # NO token price is 1.0 - YES price
                yes_price = self.current_yes_price + price_change
                yes_price = max(0.001, min(0.999, yes_price))
                self.current_yes_price = yes_price
                price = 1.0 - yes_price
                price = max(0.001, min(0.999, price))
            
            # Random size
            size = self.random.uniform(self.params.min_trade_size, self.params.max_trade_size)
            
            # Random side
            side = self.random.choice(["BUY", "SELL"])
            
            trades.append(SyntheticTrade(ts=ts, price=price, size=size, side=side, token=token))
        
        return trades
    
    def generate_book_snapshots(
        self, 
        n_snapshots: int, 
        start_ts: float, 
        end_ts: float,
        yes_trades: list[SyntheticTrade],
    ) -> list[SyntheticBookUpdate]:
        """Generate synthetic book snapshots."""
        snapshots = []
        ts_step = (end_ts - start_ts) / n_snapshots
        
        for i in range(n_snapshots):
            ts = start_ts + i * ts_step
            ts = max(start_ts, min(end_ts, ts))
            
            # Update price based on recent trades
            recent_trades = [t for t in yes_trades if start_ts <= t.ts <= ts]
            if recent_trades:
                latest_price = recent_trades[-1].price
                self.current_yes_price = latest_price
            
            # Generate book around current price
            price = self.current_yes_price
            spread = self.random.uniform(0.002, 0.01)
            
            bids = []
            asks = []
            
            # Generate 5-10 bid levels
            n_levels = self.random.randint(5, 10)
            for j in range(n_levels):
                bid_price = price - (j + 1) * 0.001
                bid_price = max(0.001, bid_price)
                bid_size = self.random.uniform(
                    self.params.min_book_depth * 0.1, 
                    self.params.max_book_depth
                )
                bids.append((bid_price, bid_size))
            
            # Generate 5-10 ask levels
            for j in range(n_levels):
                ask_price = price + (j + 1) * 0.001
                ask_price = min(0.999, ask_price)
                ask_size = self.random.uniform(
                    self.params.min_book_depth * 0.1, 
                    self.params.max_book_depth
                )
                asks.append((ask_price, ask_size))
            
            snapshots.append(SyntheticBookUpdate(
                ts=ts,
                asset_id=self.params.yes_token,
                bids=bids,
                asks=asks
            ))
            
            # Also generate NO token book
            no_price = 1.0 - price
            no_spread = spread
            no_bids = []
            no_asks = []
            
            for j in range(n_levels):
                bid_price = no_price - (j + 1) * 0.001
                bid_price = max(0.001, bid_price)
                bid_size = self.random.uniform(
                    self.params.min_book_depth * 0.1, 
                    self.params.max_book_depth
                )
                no_bids.append((bid_price, bid_size))
            
            for j in range(n_levels):
                ask_price = no_price + (j + 1) * 0.001
                ask_price = min(0.999, ask_price)
                ask_size = self.random.uniform(
                    self.params.min_book_depth * 0.1, 
                    self.params.max_book_depth
                )
                no_asks.append((ask_price, ask_size))
            
            snapshots.append(SyntheticBookUpdate(
                ts=ts + 0.001,  # Slightly after YES book
                asset_id=self.params.no_token,
                bids=no_bids,
                asks=no_asks
            ))
        
        return snapshots
    
    def generate_journal(self, output_path: Path) -> Path:
        """Generate a complete synthetic journal file."""
        start_ts = 1_700_000_000.0
        end_ts = start_ts + self.params.duration_hours * 3600.0
        
        # Number of events
        n_trades = int(self.params.trades_per_hour * self.params.duration_hours)
        n_snapshots = int(self.params.book_updates_per_hour * self.params.duration_hours)
        
        print(f"Generating synthetic journal:")
        print(f"  Duration: {self.params.duration_hours} hours")
        print(f"  Trades: {n_trades}")
        print(f"  Book snapshots: {n_snapshots}")
        
        # Generate trades
        yes_trades = self.generate_trades(n_trades, start_ts, end_ts)
        
        # Generate book snapshots
        snapshots = self.generate_book_snapshots(
            n_snapshots, start_ts, end_ts, yes_trades
        )
        
        # Combine all events
        rows = []
        
        # Add initial book snapshots
        for snap in snapshots[:2]:
            rows.append(self._book_to_journal_row(snap))
        
        # Add market meta
        rows.append(self._create_market_meta_row(start_ts))
        
        # Interleave trades and book updates
        all_events = []
        for snap in snapshots[2:]:
            all_events.append(("book", snap.ts, snap))
        for trade in yes_trades:
            all_events.append(("trade", trade.ts, trade))
        
        # Sort by timestamp
        all_events.sort(key=lambda x: x[1])
        
        for event_type, ts, data in all_events:
            if event_type == "book":
                rows.append(self._book_to_journal_row(data))
            elif event_type == "trade":
                rows.append(self._trade_to_journal_row(data))
        
        # Write journal
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        
        print(f"  Generated: {output_path}")
        print(f"  Total rows: {len(rows):,}")
        
        return output_path
    
    def _book_to_journal_row(self, snap: SyntheticBookUpdate) -> dict[str, Any]:
        """Convert book update to journal row format."""
        return {
            "ts": snap.ts,
            "kind": "book",
            "data": {
                "market": self.params.condition_id,
                "asset_id": snap.asset_id,
                "bids": [
                    {"price": f"{p:.4f}", "size": f"{s:.2f}"}
                    for p, s in snap.bids
                ],
                "asks": [
                    {"price": f"{p:.4f}", "size": f"{s:.2f}"}
                    for p, s in snap.asks
                ],
                "timestamp": str(int(snap.ts * 1000)),
                "tick_size": f"{self.params.tick_size}",
            }
        }
    
    def _trade_to_journal_row(self, trade: SyntheticTrade) -> dict[str, Any]:
        """Convert trade to journal row format."""
        asset_id = trade.token if trade.token else self.params.yes_token
        price = trade.price
        
        return {
            "ts": trade.ts,
            "kind": "last_trade_price",
            "data": {
                "market": self.params.condition_id,
                "asset_id": asset_id,
                "price": f"{price:.4f}",
                "size": f"{trade.size:.2f}",
                "side": trade.side,
                "timestamp": str(int(trade.ts * 1000)),
            }
        }
    
    def _create_market_meta_row(self, ts: float) -> dict[str, Any]:
        """Create a market meta row for the journal."""
        return {
            "ts": ts,
            "kind": "market_meta",
            "data": {
                "condition_id": self.params.condition_id,
                "market": self.params.market_id,
                "question": "24 Hour Backtest Market",
                "slug": "24h-backtest",
                "tokens": [
                    {"token_id": self.params.yes_token, "name": "Yes"},
                    {"token_id": self.params.no_token, "name": "No"},
                ],
                "tick_size": self.params.tick_size,
                "rewards_min_size": 10.0,
                "rewards_max_spread": 5.0,
                "rewards_daily_rate": 200.0,
                "maker_fee_bps": 0,
                "taker_fee_bps": 400,
                "rebate_rate": 0.25,
            }
        }


def create_24h_journal(output_path: Path, seed: int = 42) -> Path:
    """Create a synthetic 24-hour journal with realistic market data."""
    params = SyntheticMarketParams(
        duration_hours=24.0,
        trades_per_hour=120,  # 2 trades per minute
        book_updates_per_hour=60,  # 1 book update per minute
        volatility_sigma=0.02,  # Higher vol for more action
        min_trade_size=5.0,
        max_trade_size=1000.0,
        min_book_depth=1000.0,
        max_book_depth=20000.0,
    )
    
    generator = SyntheticMarketGenerator(params)
    generator.set_seed(seed)
    return generator.generate_journal(output_path)


def scale_profile_for_bankroll(
    profile: StrategyProfile,
    bankroll: float,
    *,
    min_order_size: float = 5.0,
    rewards_min_size: float = 10.0,
    typical_price: float = 0.5,
) -> StrategyProfile:
    """Scale a profile's sizing parameters for a given bankroll.

    Ensures base_size can fund at least one exchange-minimum order when
    capital allows; otherwise leaves sizes but caller must check capital gate.
    """
    from copy import deepcopy

    scaled = deepcopy(profile)
    ref_bankroll = max(scaled.bankroll_usdc, 100.0)
    scale_factor = bankroll / ref_bankroll

    scaled.base_size_usdc = max(1.0, scaled.base_size_usdc * scale_factor)
    scaled.q_max_usdc = max(1.0, scaled.q_max_usdc * scale_factor)
    scaled.bankroll_usdc = bankroll

    # Floor: at least one min-share order notional if capital can support it
    min_shares = max(min_order_size, rewards_min_size)
    min_notional = min_shares * typical_price
    cap = check_capital_feasibility(
        bankroll=bankroll,
        exchange_min_shares=min_order_size,
        reward_min_shares=rewards_min_size,
        typical_price=typical_price,
        layers=max(1, scaled.layers),
    )
    if cap.ok and scaled.base_size_usdc < min_notional:
        scaled.base_size_usdc = min_notional
        scaled.q_max_usdc = max(scaled.q_max_usdc, min_notional * 2)
    return scaled


def get_market_meta(params: SyntheticMarketParams) -> MarketMeta:
    """Create MarketMeta for synthetic market."""
    return MarketMeta(
        condition_id=params.condition_id,
        question="24 Hour Backtest Market",
        slug="24h-backtest",
        tokens=(
            TokenMeta(params.yes_token, "Yes"),
            TokenMeta(params.no_token, "No"),
        ),
        tick_size=params.tick_size,
        neg_risk=False,
        min_order_size=1.0,
        rewards_min_size=10.0,
        rewards_max_spread=5.0,
        rewards_daily_rate=200.0,
        maker_fee_bps=0,
        taker_fee_bps=400,
        fees_enabled=True,
        end_date_iso=None,
        event_id=None,
        rebate_rate=0.25,
        liquidity_num=10000.0,
    )


def run_single_market_backtest(
    journal_path: Path,
    profile: StrategyProfile,
    bankroll: float,
    out_dir: Path,
    meta: MarketMeta,
) -> tuple[ReplayResult, MetricsReport]:
    """Run backtest for a single market."""
    metrics_path = out_dir / "metrics-backtest.jsonl"
    
    print(f"\n{'='*60}")
    print(f"Running backtest: {meta.condition_id}")
    print(f"Bankroll: ${bankroll}")
    print(f"Profile: base_size=${profile.base_size_usdc}, q_max=${profile.q_max_usdc}")
    print(f"{'='*60}")
    
    # Capital gate: never claim success when bankroll cannot fund an order
    cap = check_capital_feasibility(
        bankroll=bankroll,
        exchange_min_shares=meta.min_order_size,
        reward_min_shares=meta.rewards_min_size,
        typical_price=0.5,
        layers=max(1, profile.layers),
    )
    print(f"\nCapital check: ok={cap.ok} — {cap.reason}")
    if not cap.ok:
        print("STATUS: INSUFFICIENT_CAPITAL — refusing silent zero-trade run")

    # Run replay
    result = run_replay(
        journal_path=journal_path,
        meta=meta,
        profile=profile,
        metrics_path=metrics_path,
    )
    
    print(f"\nReplay Statistics:")
    print(f"  Events read: {result.events_read:,}")
    print(f"  Events applied: {result.events_applied:,}")
    print(f"  Recomputes: {result.recomputes:,}")
    print(f"  Quotes: {result.n_quote:,}")
    print(f"  Cancels: {result.n_cancel:,}")
    print(f"  Marks: {result.n_mark:,}")
    print(f"  Fills: {result.n_fill:,}")
    print(f"  Equity (ledger): ${result.final_equity:.4f}")
    print(f"  State divergence: {result.state_divergence_events}")
    print(f"  Fills after cancel: {result.fills_after_cancel}")
    print(f"  Overfills: {result.overfills}")

    validity = evaluate_benchmark(
        n_quote=result.n_quote,
        n_fill=result.n_fill,
        n_mark=result.n_mark,
        n_markets=1,
        runtime_s=24.0 * 3600.0,
        n_trade_prints=result.events_applied,
        capital_ok=cap.ok,
        state_divergence_events=result.state_divergence_events,
        fills_after_cancel=result.fills_after_cancel,
        overfills=result.overfills,
        cfg=ValidityConfig(
            min_quotes=50,
            min_fills=10,
            min_marks=20,
            min_runtime_s=60.0,
            min_trade_prints=20,
        ),
    )
    print(f"\nBenchmark validity: {validity.status.value}")
    for reason in validity.reasons:
        print(f"  - {reason}")
    if validity.status is not BenchmarkStatus.PASS:
        print(
            "STATUS: NOT A FINANCIAL PASS "
            f"({validity.status.value}) — do not trust PnL claims"
        )
    
    # Analyze metrics
    report = analyze(metrics_path)
    
    print(f"\nMetrics Analysis:")
    print(f"  Fills: {report.n_fill}")
    print(f"  Realized Spread: ${report.realized_spread_usdc:.4f}")
    print(f"  Reward Accrual: ${sum(report.reward_accrual_usdc.values()):.4f}")
    print(f"  Inventory Drift: {report.inventory_drift_abs_peak:.4f}")
    print(f"  Markout (30s): {report.markout.get('30s', 0.0):.6f}")
    
    # Attach validity for report writers (duck-typed)
    result.validity = validity  # type: ignore[attr-defined]
    return result, report


def estimate_daily_return(
    report: MetricsReport,
    bankroll: float,
    runtime_hours: float,
) -> dict[str, float]:
    """Estimate daily return metrics."""
    total_reward = sum(report.reward_accrual_usdc.values())
    total_spread = report.realized_spread_usdc
    total_pnl = total_spread + total_reward
    
    # Normalize to 24 hours
    if runtime_hours > 0:
        daily_factor = 24.0 / runtime_hours
    else:
        daily_factor = 1.0
    
    daily_pnl = total_pnl * daily_factor
    daily_return_pct = (daily_pnl / bankroll) * 100 if bankroll > 0 else 0.0
    
    return {
        "total_pnl_usdc": total_pnl,
        "total_spread_usdc": total_spread,
        "total_rewards_usdc": total_reward,
        "daily_pnl_usdc": daily_pnl,
        "daily_return_pct": daily_return_pct,
        "runtime_hours": runtime_hours,
    }


def write_report(
    out_dir: Path,
    result: ReplayResult,
    report: MetricsReport,
    params: dict,
    daily_estimates: dict,
) -> Path:
    """Write comprehensive backtest report."""
    report_path = out_dir / "BACKTEST_REPORT.md"
    
    with report_path.open("w") as f:
        f.write("# 24-Hour Paper Backtest Report\n\n")
        f.write(f"**Generated:** {datetime.now().isoformat()}\n\n")
        
        f.write("## Configuration\n\n")
        f.write(f"- Bankroll: ${params['bankroll']:.2f}\n")
        f.write(f"- Profile: {params['profile']}\n")
        f.write(f"- Duration: {params['duration_hours']} hours\n")
        f.write(f"- Market ID: {params['market_id']}\n\n")
        
        f.write("## Replay Statistics\n\n")
        f.write(f"- Events read: {result.events_read:,}\n")
        f.write(f"- Events applied: {result.events_applied:,}\n")
        f.write(f"- Recomputes: {result.recomputes:,}\n")
        f.write(f"- Quotes: {result.n_quote:,}\n")
        f.write(f"- Cancels: {result.n_cancel:,}\n")
        f.write(f"- Marks: {result.n_mark:,}\n")
        f.write(f"- Fills: {result.n_fill:,}\n\n")
        
        f.write("## PnL Analysis\n\n")
        f.write(f"- Total Spread PnL: ${daily_estimates['total_spread_usdc']:.4f}\n")
        f.write(f"- Total Reward Accrual: ${daily_estimates['total_rewards_usdc']:.4f}\n")
        f.write(f"- Total PnL: ${daily_estimates['total_pnl_usdc']:.4f}\n")
        f.write(f"- Daily PnL (extrapolated): ${daily_estimates['daily_pnl_usdc']:.4f}\n")
        f.write(f"- Daily Return %: {daily_estimates['daily_return_pct']:.2f}%\n")
        f.write(f"- Runtime Hours: {daily_estimates['runtime_hours']:.2f}\n\n")
        
        if report.n_fill > 0:
            f.write("## Fill Statistics\n\n")
            f.write(f"- Fill Rate: {(report.n_fill / result.n_quote * 100):.2f}%\n")
            avg_spread = daily_estimates['total_spread_usdc'] / report.n_fill
            f.write(f"- Avg Spread per Fill: ${avg_spread:.4f}\n\n")
        
        f.write("## Regime Statistics\n\n")
        f.write("*(From replay metrics)*\n\n")
        
        f.write("## Notes\n\n")
        f.write("- This is a synthetic backtest with simulated fills\n")
        f.write("- Real market behavior may differ\n")
        f.write("- Use for strategy validation, not live performance prediction\n")
    
    return report_path


def main():
    parser = argparse.ArgumentParser(
        description="Run 24-hour paper backtest with $30 starting capital"
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=30.0,
        help="Starting capital in USD (default: 30)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="live-tiny",
        help="Strategy profile name (default: live-tiny)",
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=24.0,
        help="Backtest duration in hours (default: 24)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("backtest_24h"),
        help="Output directory (default: backtest_24h)",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("livecfg"),
        help="Config directory (default: livecfg)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        default=True,
        help="Use synthetic journal (default: True)",
    )
    
    args = parser.parse_args()
    
    print("="*60)
    print("24-HOUR PAPER BACKTEST")
    print("="*60)
    
    # Create output directory
    args.out_dir.mkdir(parents=True, exist_ok=True)
    
    # Create synthetic journal
    print(f"\nGenerating synthetic {args.duration_hours}h journal...")
    params = SyntheticMarketParams(
        duration_hours=args.duration_hours,
        trades_per_hour=120,
        book_updates_per_hour=60,
        volatility_sigma=0.02,
    )
    generator = SyntheticMarketGenerator(params)
    generator.set_seed(args.seed)
    journal_path = args.out_dir / "journal.jsonl"
    journal_path = generator.generate_journal(journal_path)
    
    # Get market meta
    meta = get_market_meta(params)
    
    # Load or create profile
    print(f"\nLoading profile '{args.profile}'...")
    try:
        cfg = Config.load(args.config_dir, load_env=False)
        if args.profile in cfg.profiles:
            profile = cfg.profiles[args.profile]
        else:
            print(f"Profile '{args.profile}' not found, using defaults")
            profile = StrategyProfile()
    except Exception as e:
        print(f"Error loading config: {e}, using defaults")
        profile = StrategyProfile()
    
    # Scale profile to bankroll
    profile = scale_profile_for_bankroll(profile, args.bankroll)
    
    # Run backtest
    print(f"\nRunning backtest with ${args.bankroll} bankroll...")
    result, report = run_single_market_backtest(
        journal_path=journal_path,
        profile=profile,
        bankroll=args.bankroll,
        out_dir=args.out_dir,
        meta=meta,
    )
    
    # Calculate daily estimates
    runtime_hours = args.duration_hours  # We know this from synthetic
    daily_estimates = estimate_daily_return(report, args.bankroll, runtime_hours)
    
    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    print(f"Total PnL: ${daily_estimates['total_pnl_usdc']:.4f}")
    print(f"Daily PnL (extrapolated): ${daily_estimates['daily_pnl_usdc']:.4f}")
    print(f"Daily Return: {daily_estimates['daily_return_pct']:.2f}%")
    print(f"Fills: {report.n_fill}")
    print(f"Quotes: {result.n_quote}")
    print(f"Fill Rate: {(report.n_fill / result.n_quote * 100):.2f}%" if result.n_quote > 0 else "Fill Rate: N/A")
    
    # Write report
    report_params = {
        "bankroll": args.bankroll,
        "profile": args.profile,
        "duration_hours": args.duration_hours,
        "market_id": params.condition_id,
    }
    report_path = write_report(
        args.out_dir,
        result,
        report,
        report_params,
        daily_estimates,
    )
    
    print(f"\n{'='*60}")
    print("COMPLETE")
    print(f"{'='*60}")
    print(f"Report: {report_path}")
    print(f"Metrics: {args.out_dir / 'metrics-backtest.jsonl'}")
    print(f"Journal: {journal_path}")
    print(f"{'='*60}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
