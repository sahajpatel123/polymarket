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

**Size:** `base_size_usdc / price`, scaled by regime (TRENDING → 0.35), toxicity
`1/(1+12·tox)`, risk headroom, and inventory taper `(1 − |u|)` on the adding
side. Soft cap `q_soft_frac`: stop adding YES when `u ≥ q_soft_frac`, stop
adding NO when `u ≤ −q_soft_frac`. REDUCE_ONLY posts exits only.

**Placement** (`_place_bid`): never bid above `FV − min_edge_ticks·tick`, join
the best bid rather than jump it, never cross the ask. Layers step away from
the touch; each order is bumped toward `rewards_min_size · reward_size_mult`
so reward-eligible resting orders actually score.

**Exits** (`_maybe_exit`): SELL held inventory at a maker price between
`token_FV + δ` and `best_bid + tick`, walked by urgency ∈ [0,1].
REDUCE_ONLY forces urgency ≥ 0.5. Urgency is computed in the engine from
hold time and `exit_urgency_s` (base), with toxicity bumps for adverse fills.

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

Paper requotes log both `flowz` and `vol_ratio` so
`scripts/paper_regime_report.py` can attribute TRENDING as flow_only /
vol_only / both (C-01 evidence). Thresholds themselves are unchanged.

**Sweep detection** lives in `Engine._on_trade`, not in the regime module: a
print must be ≥ `event_sweep_mult · (base_size_usdc/price)` **and** ≥
`event_sweep_frac` of near-touch depth on the consumed side.

> **Note:** `event_sweep_levels` has been removed from config — sweep
> depth uses near-touch sizing via `event_sweep_frac` / `event_sweep_mult`
> only.

**Resolved markets:** `RegimeInputs.market_resolved` is set to `cid in self._halted`
by the engine, so closed / not-accepting markets flow through the HALTED regime
via the resolved field (in addition to the blind/stale path).

> **Note:** `end_date_taper_days` remains on `StrategyProfile` for TOML compat
> but is unused by the live path. Lifecycle uses only `reduce_only_hours` and
> `halt_before_hours`.

## Profile knobs (strategy.toml)

Every quoter knob is a field on `StrategyProfile` (`config.py`). Named
profiles live in `config/strategy.toml`. Per-market TOML extras on a
`[[markets]]` entry become overrides via `MarketEntry.overrides`.

Shipped profiles today: `newsom-mm`, `political-longdated`, `political-hot`,
`romania-pm` (under `config/`), and `live-tiny` (under `livecfg/`). The CLI
default (`--profile political-longdated`) resolves to the in-repo profile.

## Fill model deployment gate

**Files:** `strategy/fill_model.py`, `engine.py` → `_filter_quotes_by_fill_model`

The fill model (P(fill) + E[markout] gradient-boosted trees) only filters or
sizes live quotes when `FillModel.is_deployable` — i.e. it passed a holdout
validation (`validate()`, honest 70/30 re-fit) on the **live** slice of the
training buffer with `auc ≥ model.min_auc` and `corr ≥ model.min_markout_corr`.
Until then it runs in **shadow**: decisions are logged
(`fill_model_shadow_reject`) but the empirical book-shape tree
(`quality_filter_score`, `_quality_filter_from_book`) remains the quote gate.

- Artifacts load in `Engine._load_persisted_fill_model` (before quoting).
  Load itself never deploys: only `min_live_validation_samples` live-acquired
  samples can unlock deployment, so a cold artifact cannot take over quoting.
- `_retrain_fill_model` (5 min cadence) re-trains on the merged offline+online
  buffer, re-validates the online slice, and persists **only** if deployable.
- Training provenance is tracked per sample (`offline` from the trainer,
  `online` from engine fills/kept quotes); the `source` list is persisted with
  the artifact (format v2) so deployment state survives restarts.
- **Exits are never removed**: SELL quotes on held inventory (incl.
  REDUCE_ONLY) may be sized by a deployable model but cannot be dropped by
  either gate.

Offline training: `scripts/train_fill_model.py` reconstructs fill/non-fill
labels and 30 s markouts from a raw `book`/`last_trade_price` journal, with
tape-derived features (vol_ratio, flow_z, toxicity, hours_to_resolve, regime)
so the model sees realistic variance. Reproduction:

```
uv run python scripts/train_fill_model.py \
    --journal backtest_24h/journal.jsonl --output models/fill_model.pkl
```
