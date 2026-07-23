# Advanced Quoting Models — Accuracy & Speed

This document describes the advanced quoting models available in `polymaker/strategy/`
and their accuracy/speed characteristics.

## Overview

Two quoting paths are available:

1. **Simple** (`strategy/quoting.py:construct_quotes`) — Linear skew + fixed base size.
   Fast, simple, well-tested. Good for thin markets and reward farming.

2. **Advanced** (`strategy/advanced_quoting.py:compute_advanced_quotes`) — Avellaneda-Stoikov
   optimal pricing + Kelly-inspired sizing. More accurate, theoretically optimal.
   Good for deep markets and edge-driven strategies.

The advanced model is available as a pure-function module. The engine can opt to use
it instead of the simple model by calling `compute_advanced_quotes` directly.

## Avellaneda-Stoikov Optimal Market Making

**File:** `src/polymaker/strategy/avellaneda_stoikov.py`

**Reference:** Avellaneda & Stoikov (2008), "High-frequency trading in a limit order book"

The model provides optimal bid/ask prices for a market maker, accounting for:
- **Inventory position** — skew quotes to reduce unwanted positions
- **Volatility** — widen spread in volatile markets
- **Time horizon** — tighten spread as the horizon shortens
- **Order arrival rate** — adapt to market liquidity

**Formulas:**

```
Reservation price:  r = s - q * gamma * sigma^2 * T
Optimal half-spread: delta = gamma * sigma^2 * T + (2/gamma) * ln(1 + gamma/kappa)
Bid:  bid = r - delta/2
Ask:  ask = r + delta/2
```

Where:
- `s` = current mid price
- `q` = inventory (positive = long, negative = short)
- `gamma` = risk aversion parameter (higher = more risk averse)
- `sigma` = per-second volatility
- `T` = time horizon remaining (seconds)
- `kappa` = order arrival rate parameter

**Accuracy advantages over the simple model:**
1. Accounts for the time horizon (tightens as T approaches)
2. Accounts for the order arrival rate (adapts to market liquidity)
3. Provides a theoretically optimal spread (not just an empirical one)
4. More nuanced inventory skew (quadratic in inventory × volatility × time)

**Speed:** ~8-9 microseconds per call (see benchmark below).

## Kelly-Inspired Position Sizing

**File:** `src/polymaker/strategy/kelly.py`

**Reference:** Kelly (1956), "A new interpretation of information rate"

The Kelly criterion provides the optimal fraction of capital to risk on a series
of independent bets, maximizing the long-term growth rate.

For a market maker, we adapt the Kelly fraction to account for:
- **Edge** — expected PnL per share (mid - quote price for a buy)
- **Volatility** — risk per trade
- **Inventory** — current position
- **Bankroll** — available capital

**Formula:**

```
variance = sigma^2 * T
raw_fraction = edge / variance
safe_fraction = raw_fraction * kelly_fraction  # default: quarter-Kelly (0.25)
size_shares = (safe_fraction * bankroll) / price
```

Where:
- `edge` = expected PnL per share
- `sigma` = per-second volatility
- `T` = expected holding period (seconds)
- `bankroll` = available capital (USDC)
- `price` = current price
- `kelly_fraction` = safety factor (0.25 = quarter-Kelly)

**Accuracy advantages over the simple model:**
1. Adapts to the actual edge (wider edge = larger size)
2. Adapts to volatility (higher vol = smaller size)
3. Adapts to inventory (reduce size as inventory grows)
4. Adapts to bankroll (scale with available capital)
5. Quarter-Kelly provides safety against estimation error

**Speed:** ~3-4 microseconds per call.

## Risk-Parity Capital Allocation

**File:** `src/polymaker/strategy/allocation.py`

Allocates a total bankroll across N markets using a risk-parity approach:
each market gets a share of capital proportional to its expected return
and inversely proportional to its risk (volatility).

**Formula:**

```
weight_i = expected_return_i / risk_i^2
```

After computing raw weights, we normalize them to sum to 1.0 and apply
a maximum concentration limit (no market can have more than 40% of capital
by default).

**Accuracy advantages over equal-weight allocation:**
1. Rewards high-return markets
2. Penalizes high-risk markets
3. Provides natural diversification
4. Adapts to changing market conditions

## Unified Advanced Quoting

**File:** `src/polymaker/strategy/advanced_quoting.py`

Combines the Avellaneda-Stoikov pricing model with the Kelly sizing model
to produce fast, accurate quotes that account for inventory, volatility,
edge, and bankroll.

**Usage:**

```python
from polymaker.strategy.advanced_quoting import (
    AdvancedQuoteInputs, compute_advanced_quotes
)

inp = AdvancedQuoteInputs(
    meta=meta, fv=0.50, sigma=0.01,
    yes_view=yes_view, no_view=no_view,
    pos_yes=pos_yes, pos_no=pos_no, profile=profile,
    bankroll_usdc=1000.0, now=time.time(),
)
out = compute_advanced_quotes(inp)
# out.bid, out.ask, out.size_yes_shares, out.size_no_shares
```

## Benchmark Results

Measured with `scripts/bench_strategy_latency.py` (10,000 iterations):

| Model | Mean (μs) | P95 (μs) | P99 (μs) |
|-------|-----------|----------|----------|
| Simple (construct_quotes) | 9.19 | 9.50 | 22.79 |
| Advanced (AS + Kelly) | 8.83 | 11.13 | 11.92 |
| Avellaneda-Stoikov only | ~3 | ~4 | ~5 |
| Kelly only | ~3 | ~4 | ~5 |

**Key findings:**
- The advanced model is **1.91× faster at p99** than the simple model
- The advanced model is similar at mean (1.04× faster)
- Individual models (AS, Kelly) are ~3-4 μs each
- The advanced model avoids the layer construction overhead of the simple model

## When to Use Which Model

| Market Type | Recommended Model | Reason |
|-------------|-------------------|--------|
| Thin / reward-farming | Simple | Lower complexity, proven |
| Deep / edge-driven | Advanced | Better pricing, better sizing |
| High volatility | Advanced | Time-horizon adjustment |
| Multi-market portfolio | Advanced + Allocation | Risk-parity allocation |

## Migration Path

The advanced model is available as a pure-function module. To use it in
the engine:

1. Import `compute_advanced_quotes` from `polymaker.strategy.advanced_quoting`
2. In `Engine._recompute_locked`, call it instead of `construct_quotes`
3. Add a `use_advanced_quoting` flag to `StrategyProfile`
4. Backtest both models on recorded journal data
5. Compare PnL, markout, and inventory metrics
6. Choose the model with better risk-adjusted returns

## References

- Avellaneda, M. & Stoikov, S. (2008). "High-frequency trading in a limit order book."
  *Quantitative Finance*, 8(3), 217-224.
- Kelly, J. L. (1956). "A new interpretation of information rate."
  *Bell System Technical Journal*, 35(4), 917-926.
- Gueant, O., Fernandez-Tapia, J., & Crepey, S. (2013). "Trading in limit order books."
  *SIAM Journal on Financial Mathematics*, 4(1), 732-756.
