# poly-maker

A maker-only market-making bot for **Polymarket CLOB V2**, focused on political
markets. Single async process, local-file config, typed and tested.

> [!WARNING]
> Market making on Polymarket is competitive and can lose money. This is a
> reference implementation and a research harness, not a guaranteed-profitable
> product. Test in `--paper` mode first; go live with small size.

## What it does (as implemented)

- Discovers political markets via the **Gamma API** and ranks them by a
  reward + rebate heuristic vs. spread/extremity ([docs/market-selection.md](docs/market-selection.md)).
  The live trade list is **manual** (`config/markets.toml`).
- Maintains a live order book per token from the **market WebSocket**.
- Quotes **maker-only** (post-only). Fair-value + inventory-skew strategy posts
  BUY-YES and BUY-NO, with live vol/toxicity estimators and a regime machine
  that pulls quotes on sweeps/jumps ([docs/strategy.md](docs/strategy.md)).
- Reconciles target quotes against live orders with churn tolerances
  ([docs/order-reconciliation.md](docs/order-reconciliation.md)).
- Exchange **heartbeat** dead-man switch (live mode); risk caps; daily-loss and
  order-error kill switches.
- Config + state are **local TOML + SQLite**. Journals under `journal/` for
  later analysis (replay backtester not built yet).

Agent-oriented architecture notes: [CLAUDE.md](CLAUDE.md). Live ops notes:
[TIPS.md](TIPS.md). Unattended cycles:
[AUTONOMOUS_LOOP_PROTOCOL.md](AUTONOMOUS_LOOP_PROTOCOL.md).

## Install

Uses [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync --extra dev          # install deps + dev tools
uv run polymaker --help
```

## Configure

```bash
cp .env.example .env         # then edit:
```

- `PK` — private key of the **signer** wallet
- `BROWSER_ADDRESS` — Polymarket **funder** address (deposit/proxy wallet that
  holds pUSD for `signature_type` 1/2/3; for EOA type 0 this is usually the
  same as the signer)

Optional: `ALERT_WEBHOOK_URL`, `ALL_PROXY` / `HTTPS_PROXY`, builder/relayer
creds for deposit-wallet merges (see `.env.example`).

TOML under [`config/`](config/):

| File | Role |
|------|------|
| `config.toml` | wallet / engine / risk / execution / paths |
| `strategy.toml` | named profiles (`newsom-mm`, `romania-pm` shipped) |
| `markets.toml` | trade list (`slug`, `profile`, `enabled`, optional overrides) |

`signature_type` in `config.toml`: `0` EOA, `1` email/magic proxy, `2` Gnosis
Safe, `3` POLY_1271 deposit wallet (current Polymarket default). Wrong type
fails auth — `polymaker doctor` / `livetest` catch this.

For a tiny defensive self-test layout, use `--config-dir livecfg` (profile
`live-tiny`).

## Use

```bash
# 1. discover + rank political markets (writes state.db + markets.csv)
uv run polymaker scan
uv run polymaker markets

# 2. add markets to the trade list (profile must exist in strategy.toml)
uv run polymaker markets-add <slug> --profile newsom-mm

# 3. paper: live books + full quote pipeline, no orders posted
uv run polymaker run --paper

# 4. preflight the wallet before going live
uv run polymaker doctor

# 5. self-tests against the exchange
uv run polymaker livetest      # deep post-only + cancel (no intentional fill)
uv run polymaker moneydoctor   # limit rest + market buy/sell; spends a little

# 6. go live (ONE process only — see TIPS.md)
uv run polymaker run

# ops
uv run polymaker status        # positions / open orders from SQLite
uv run polymaker pnl           # latest equity / daily PnL snapshot
uv run polymaker cancel-all    # panic button
```

### Paper mode details

`--paper` exercises market data → estimators → regime → quoting → reconcile
against the **live** WebSocket book. The gateway invents `paper-*` order ids and
never posts. It does **not** simulate fills, inventory, toxicity from fills, or
user-stream updates. Use it to validate quote placement and regime behavior in
logs (`logs/paper.jsonl`), not as a PnL backtest.

## Architecture

```
market WS ─▶ OrderBook ─▶ (wake) ─▶ Quoter ─▶ strategy (pure) ─▶ reconcile ─▶ ExecutionGateway
user WS   ─▶ StateStore                                         RiskManager ┘   (post-only, heartbeat)
Gamma     ─▶ Catalog/scanner ─▶ SQLite            periodic REST reconcile ┘
```

One async event loop. Strategy is a pure function
`(book, inventory, params, clock) → TargetQuotes`. `ExecutionGateway` wraps
`py-clob-client-v2` (V2 EIP-712 signing) on a thread pool. State lives in one
SQLite file; raw events journal to `journal/` when enabled.

## Strategy (summary)

Maker-only, both sides as USDC bids:

- **Fair value** — depth-weighted microprice + EWMA signed-flow nudge
- **Quotes** — reservation `r = FV − skew(inventory)`; half-spread
  `δ = base + c_vol·σ + c_tox·toxicity`; BUY-YES at `r − δ`, BUY-NO at
  `(1 − r) − δ`
- **Inventory skew** — long YES → lower YES bid / higher NO bid; soft cap pulls
  the adding side
- **Regime** — `QUIET` / `TRENDING` / `EVENT` / `REDUCE_ONLY` / `HALTED`
- **Risk** — per-market and total inventory caps, neg-risk event-group cap,
  daily-loss kill, WS/user/heartbeat blinds

Full math and known unused knobs: [docs/strategy.md](docs/strategy.md).

## Develop

```bash
uv run pytest                 # unit suite (offline)
POLYMAKER_LIVE=1 uv run pytest tests/test_live_marketdata.py   # live WS
uv run ruff check src tests
uv run mypy src
```

## Status

Implemented end to end: config, catalog/scanner, order book + analytics,
strategy (FV, vol/toxicity, regime, quoting), state store, execution gateway +
reconciler + heartbeat, market/user websockets, risk manager, merger (EOA /
Safe / deposit-wallet with builder creds), engine, CLI, paper mode, journal
capture. Offline test suite under `tests/`; ruff + mypy strict configured.

Not built: journal/L2 **replay backtester**; external news/cross-venue feeds;
engine hot-reload of `markets.toml` (`watchfiles` is listed but unused).

## License

MIT
