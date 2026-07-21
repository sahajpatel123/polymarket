# Strategy: fair value, inventory skew, regime

The strategy layer is pure and deterministic: given book state, inventory,
estimators, and a profile, it returns `TargetQuotes`. All math lives under
`src/polymaker/strategy/`. The engine (`engine.py`) owns I/O, wakes, and wiring.

## Call path

```
Engine._recompute_locked
  → OrderBook.microprice(levels)
  → compute_fair_value(micro, flow_z, tick)     # strategy/quoting.py
  → VolEstimator / FlowEstimator / MarkoutTracker updates  # strategy/estimators.py
  → RiskManager.evaluate → size_scale / halt / reduce_only
  → RegimeMachine.decide                        # strategy/regime.py
  → construct_quotes(QuoteInputs)               # strategy/quoting.py
  → reconcile(targets, live, …)                 # execution/reconciler.py
```

## Fair value

**File:** `strategy/quoting.py` → `compute_fair_value`

1. **Microprice** (`marketdata/orderbook.py` → `OrderBook.microprice`):
   depth-weighted mid over the top `micro_levels` (profile default 3). Bid size
   weights the ask price and ask size weights the bid — price is pulled toward
   the thinner side.
2. **Flow nudge:** `FV = microprice + 0.5 · flow_z · tick`, clamped to
   `(tick, 1−tick)`.
3. `flow_z` comes from `FlowEstimator` (EWMA of signed aggressor size /
   EWMA of |size|), fed by market-WS trade prints in `Engine._on_trade`.

The engine skips a tick if the YES book is empty, one-sided, or crossed/locked.

## Inventory skew and quote construction

**File:** `strategy/quoting.py` → `construct_quotes`

Inventory is YES-equivalent shares: `net = pos_yes − pos_no`. Utilization:

```
u = clamp(net / (q_max_usdc / FV), −1, 1)
```

Reservation and half-spread:

```
skew  = gamma · σ_short · u
δ     = max(delta_min_ticks·tick + c_vol·σ + c_tox·toxicity, tick)
        # in QUIET, also clamped into the liquidity-rewards band
r     = FV − skew
YES bid target = r − δ
NO  bid target = (1 − r) − δ
```

Both entry legs are **BUY** (USDC collateral). A filled pair locks edge
`1 − p − q` and can be merged back to collateral.

**Size:** `base_size_usdc / price`, scaled by regime (TRENDING → 0.5), toxicity
`1/(1+10·tox)`, risk headroom, and inventory taper `(1 − |u|)` on the adding
side. Soft cap `q_soft_frac`: stop adding YES when `u ≥ q_soft_frac`, stop
adding NO when `u ≤ −q_soft_frac`. REDUCE_ONLY posts exits only.

**Placement** (`_place_bid`): never bid above `FV − min_edge_ticks·tick`, join
the best bid rather than jump it, never cross the ask. Layers step away from
the touch; each order is bumped toward `rewards_min_size · reward_size_mult`
so reward-eligible resting orders actually score.

**Exits** (`_maybe_exit`): SELL held inventory at a maker price between
`token_FV + δ` and `best_bid + tick`, walked by `exit_urgency ∈ [0,1]`.
REDUCE_ONLY forces urgency ≥ 0.5.

> **Gap:** `QuoteInputs.yes_exit_urgency` / `no_exit_urgency` default to `0.0`.
> The engine never computes hold-time / adverse-drift urgency from
> `exit_urgency_s`. That profile knob is currently unused by the live path.

## Online estimators

**File:** `strategy/estimators.py`

| Estimator | Input | Output used by |
|-----------|--------|----------------|
| `VolEstimator` | FV changes | `σ_short` in δ and skew; `short/long` ratio → TRENDING |
| `FlowEstimator` | trade prints | `flow_z` → FV nudge and TRENDING |
| `MarkoutTracker` | fills + FV after horizon | `toxicity = max(0, −markout)` → widen δ, shrink size |

All EWMAs are **time-decayed** (half-life in seconds), not sample-count based.

## Regime machine

**File:** `strategy/regime.py` → `RegimeMachine.decide`

Priority (highest first):

| Regime | Trigger | Quoting effect |
|--------|---------|----------------|
| `HALTED` | risk halt, WS/user/heartbeat blind, resolved, `hours_to_end ≤ halt_before_hours` | empty targets → cancel all |
| `EVENT` | sweep flag, FV jump ≥ `event_jump_ticks`, or cooloff | empty targets; cooloff `event_cooloff_s` |
| `REDUCE_ONLY` | risk reduce-only, `inventory_util ≥ 1`, or near end | exits only |
| `TRENDING` | `\|flow_z\| ≥ trend_flow_z` or `vol_ratio ≥ trend_vol_ratio` | half size |
| `QUIET` | default | full size; δ clamped into reward band |

**Sweep detection** lives in `Engine._on_trade`, not in the regime module: a
print must be ≥ `event_sweep_mult · (base_size_usdc/price)` **and** ≥
`event_sweep_frac` of near-touch depth on the consumed side.

**Resolved markets:** `RegimeInputs.market_resolved` is never set by the
engine. Closed / not-accepting markets are instead added to `Engine._halted`
via metadata refresh, which feeds the blind/HALTED path.

> **Gap:** `end_date_taper_days` is on `StrategyProfile` but unused. Lifecycle
> tapering uses only `reduce_only_hours` and `halt_before_hours`.

## Profile knobs (strategy.toml)

Every quoter knob is a field on `StrategyProfile` (`config.py`). Named
profiles live in `config/strategy.toml`. Per-market TOML extras on a
`[[markets]]` entry become overrides via `MarketEntry.overrides`.

Shipped profiles today: `newsom-mm`, `romania-pm` (under `config/`), and
`live-tiny` (under `livecfg/`). There is no `political-longdated` /
`political-hot` profile in-repo — create one before using those names.
