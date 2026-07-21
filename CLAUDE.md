# CLAUDE.md — polymaker agent guide

Maker-only market-making bot for **Polymarket CLOB V2** (package `polymaker`,
v2.0.0). Single async Python process. Config is local TOML + `.env`; state is
SQLite; journals go under `journal/`.

This file is for agents and operators working in the repo. Prefer reading the
code paths named below over inventing behavior. **Do not change strategy or
execution logic without validating against paper/journal data first.**

Unattended cycles: follow [AUTONOMOUS_LOOP_PROTOCOL.md](AUTONOMOUS_LOOP_PROTOCOL.md)
and append to [CHANGELOG_AGENT.md](CHANGELOG_AGENT.md). Tier-2 gate:
`uv run python scripts/paper_data_gate.py`.

## Repository map

```
config/                 # default TOML: config.toml, strategy.toml, markets.toml
livecfg/                # small live-test config (profile live-tiny)
src/polymaker/
  cli.py                # typer entry: scan, markets, run, doctor, …
  config.py             # pydantic models over TOML + Secrets from .env
  domain.py             # Side, Regime, MarketMeta, Quote, TargetQuotes, …
  engine.py             # async orchestration (the wiring)
  strategy/
    quoting.py          # FV + inventory skew + construct_quotes (PURE)
    estimators.py       # vol / flow / markout EWMAs (PURE)
    regime.py           # RegimeMachine (PURE)
  marketdata/           # WS order books, microprice, trade prints
  catalog/              # Gamma scan, scoring, SQLite catalog
  execution/
    gateway.py          # py-clob-client-v2 wrapper; paper mode; heartbeat
    reconciler.py       # target vs live → cancel/place plan (PURE)
  risk/manager.py       # caps, daily-loss kill, error-rate halt
  state/                # SQLite positions/orders/PnL + user-event tracker
  userstream/           # authenticated user WS (fills / order updates)
  merge.py              # YES+NO → collateral (EOA / Safe / deposit wallet)
  journal.py            # raw WS/order append log
docs/                   # subsystem docs (linked below)
TIPS.md                 # operator field notes from a live session
tests/                  # offline unit suite (~113 tests)
```

## Architecture (data flow)

```
market WS ─▶ OrderBook ─▶ (wake) ─▶ Quoter ─▶ strategy (pure) ─▶ reconcile ─▶ ExecutionGateway
user WS   ─▶ StateStore                                         RiskManager ┘   (post-only, heartbeat)
Gamma     ─▶ Catalog/scanner ─▶ SQLite            periodic REST reconcile ┘
```

One event loop. Per enabled market: a supervised quoter task woken by book/fill
events (debounced) plus a slow baseline tick. Strategy is
`(book, inventory, params, clock) → TargetQuotes`. Engine owns I/O, locks,
estimators, regime cooloffs, and risk.

Subsystem docs:

- [Strategy math (FV, skew, regime)](docs/strategy.md)
- [Market selection / scoring](docs/market-selection.md)
- [Order reconciliation / churn](docs/order-reconciliation.md)

## Where the fair-value and inventory-skew math lives

| Concern | Location |
|---------|----------|
| Microprice | `marketdata/orderbook.py` → `OrderBook.microprice` |
| FV = micro + flow nudge | `strategy/quoting.py` → `compute_fair_value` |
| Skew, δ, BUY-YES / BUY-NO, layers, exits | `strategy/quoting.py` → `construct_quotes` |
| σ, flow_z, toxicity | `strategy/estimators.py` |
| Regime priority / cooloff | `strategy/regime.py` → `RegimeMachine` |
| Sweep flag (feeds EVENT) | `engine.py` → `_on_trade` |
| Wiring / marks / wakes | `engine.py` → `_recompute_locked` |

Formulas and gaps (unused knobs, etc.): **[docs/strategy.md](docs/strategy.md)**.

## Config / parameter surface

**Secrets (`.env` only):** `PK`, `BROWSER_ADDRESS`; optional `POLYGON_RPC`,
`ALL_PROXY` / `HTTPS_PROXY`, `ALERT_WEBHOOK_URL`, builder/relayer creds for
deposit-wallet merges. Template: `.env.example`.

**`config/config.toml`**

| Section | Knobs that matter |
|---------|-------------------|
| `[wallet]` | `signature_type` (0 EOA / 1 magic / 2 Safe / 3 deposit), hosts, RPC |
| `[engine]` | `debounce_ms`, `quoter_tick_s`, `reconcile_interval_s`, `catalog_refresh_s`, `heartbeat`, `heartbeat_interval_s`, `journal`, `loop` |
| `[risk]` | exposure/event/market caps, `daily_loss_kill_usdc`, WS/user/heartbeat blind thresholds, `max_order_error_rate` |
| `[execution]` | `post_only`, rate budget fraction, batch size |
| `[paths]` | `db`, `journal_dir`, `log_dir` |

**`config/strategy.toml`** — named `StrategyProfile`s. Every quoter knob is a
field on `StrategyProfile` in `config.py` (`extra="forbid"`). Shipped:
`newsom-mm`, `romania-pm`. CLI default profile name `political-longdated` is
**not** defined in-repo — add a profile or pass `--profile newsom-mm` (etc.).

**`config/markets.toml`** — trade list: `slug`, `profile`, `enabled`, plus any
extra keys as per-market profile overrides.

Load path: `Config.load(config_dir)` (`cli` uses `--config-dir`, default
`config`). Use `livecfg/` for the tiny live self-test layout.

## Kill switch and dead-man switch

### Daily-loss / global kill (`risk/manager.py`)

`RiskManager.global_halt()` returns halt when:

1. `kill()` was called → reason `manual_kill`
2. `daily_pnl <= -daily_loss_kill_usdc` → `daily_loss …`
3. Rolling order-post error rate ≥ `max_order_error_rate` (after ≥20 attempts)

Equity = net cash from fills + marked inventory. `reset_day()` snapshots equity
at engine start (not a calendar rollover). `evaluate()` maps global halt →
per-market `RiskDecision(halt=True)` → regime `HALTED` → empty targets → cancels.

Hard notional caps (market / event-group / total) trigger **reduce-only**, not
global kill. Soft headroom tapers `size_scale` from 70% of each cap upward.

### Exchange heartbeat dead-man (`execution/gateway.py` + `engine.py`)

- Live mode only (`paper` skips heartbeat and user WS).
- Engine sends an initial heartbeat before quoters place, then
  `_heartbeat_loop` every `heartbeat_interval_s`.
- Gateway chains `post_heartbeat(previous_id)`. Failures increment
  `heartbeat_failures`.
- After `heartbeat_halt_failures` consecutive misses: engine sets
  `hb_blind` → risk/regime HALTED (exchange will also auto-cancel resting
  orders on a heartbeat gap).
- On recovery: `StateStore.clear_orders()`, REST resync, wake all quoters.

Related blinds that also HALT quoting: market WS disconnected >
`ws_stale_halt_s`, user WS disconnected > `user_ws_blind_halt_s`, market in
`Engine._halted` (closed / not accepting from Gamma metadata refresh).

Panic button: `polymaker cancel-all`.

## Paper mode (what it actually does)

`polymaker run --paper`:

- Full market-WS → book → strategy → reconcile path against **live** books
- `ExecutionGateway` fabricates `paper-*` order ids; **does not** post
- No user WS, no heartbeat loop
- No simulated fills / inventory / markouts from the tape
- REST `open_orders()` is empty → periodic reconcile ages out paper orders
  after the grace window; next requote re-places

Useful for verifying quoting/regime behavior and logs (`logs/paper.jsonl`).
Not a backtester. Journal replay backtester is **not built**.

## Unclear / underdocumented in code

Call these out before relying on them in strategy work:

1. **`exit_urgency_s` unused by engine** — exit urgency stays 0 unless
   REDUCE_ONLY bumps it to 0.5 inside `_maybe_exit`.
2. **`end_date_taper_days` unused** — only `reduce_only_hours` /
   `halt_before_hours` affect lifecycle.
3. **`_last_quote_fv` written, never read** — comment says "requote
   suppression" but no suppression logic uses it.
4. **`market_resolved` never set** — closed markets use `_halted` + blind path.
5. **Hot reload unfinished** — `Config.reload_markets` + `watchfiles` dep;
   engine does not watch `markets.toml`.
6. **README/CLI profile names** — `political-longdated` / `political-hot` are
   documented/defaulted but absent from `strategy.toml`.
7. **Exposure comments vs code** — `config.toml` comment mentions open buy
   notional in total exposure; `_total_exposure` / `_market_notional` count
   **filled inventory only** (intentionally, to avoid self-churn).
8. **Paper ≠ simulation** — see above; easy to over-read paper logs.
9. **Merge for deposit wallets** needs builder API creds; without them,
   exits are limit sells. Merge is skipped entirely in paper.
10. **Scanner score ≠ earned income** — ranking heuristic only
    ([docs/market-selection.md](docs/market-selection.md)).

## Common commands

```bash
uv sync --extra dev
cp .env.example .env          # then set PK + BROWSER_ADDRESS
uv run polymaker scan
uv run polymaker markets
uv run polymaker markets-add <slug> --profile newsom-mm
uv run polymaker run --paper
uv run polymaker doctor
uv run pytest
```

Operator live notes: [TIPS.md](TIPS.md). Human setup/feature overview:
[README.md](README.md).
