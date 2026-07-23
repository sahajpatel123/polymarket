# Self-Automated Polymarket Trader — Setup & Operations Guide

This guide covers everything you need to go from zero to a self-automated
Polymarket trader that runs unattended across multiple market categories
(politics, sports, crypto, news, etc.).

## Architecture overview

```
┌─────────────────────────────────────────────────────────┐
│                    polymaker (single process)            │
├─────────────────────────────────────────────────────────┤
│  Market WS ─▶ OrderBook ─▶ Quoter ─▶ strategy (pure)   │
│                              │                          │
│                              ▼                          │
│  User WS ─▶ StateStore ─▶ reconcile ─▶ ExecutionGateway│
│                              │           (paper or live)│
│  Gamma ──▶ Catalog/scanner ─▶ SQLite       │            │
│                              │              ▼            │
│              Paper fill sim (offline validation)        │
│              Journal replay backtester                   │
└─────────────────────────────────────────────────────────┘
```

## Step 1 — Prerequisites

1. Python 3.12+ and [uv](https://docs.astral.sh/uv/)
2. A Polymarket account (browser wallet / email / Safe / deposit wallet)
3. A server or VPS with stable internet (for unattended operation)
4. Optional: an alert webhook (Slack, Discord, ntfy, etc.)

## Step 2 — Install

```bash
git clone https://github.com/sahajpatel123/polymarket.git
cd polymarket
uv sync --extra dev
```

## Step 3 — Configure secrets

Copy the env template and fill in your wallet details:

```bash
cp .env.example .env
# then edit .env:
#   PK=0x...                       # signer private key
#   BROWSER_ADDRESS=0x...          # funder / deposit address
#   ALERT_WEBHOOK_URL=https://...  # for unattended alerts
#   POLYGON_RPC=https://...        # optional custom RPC
```

**Critical:** `signature_type` in `config/config.toml` must match your wallet
type. See the comments in that file. If unsure, run `polymaker doctor` and
`polymaker livetest` — they will tell you if you picked the wrong type.

## Step 4 — Discover markets across categories

The scanner now supports multiple Gamma tag categories in a single run:

```bash
# Politics only (default)
uv run polymaker scan

# Politics + Sports + Crypto + News
uv run polymaker scan --categories politics,sports,crypto,news

# Lower-liquidity markets included
uv run polymaker scan --categories politics,sports --min-liquidity 500

# Browse the ranked catalog
uv run polymaker markets
```

The results are written to `markets.csv` and `state.db`. Open the CSV,
pick the markets you want to trade, then add them to `config/markets.toml`:

```bash
uv run polymaker markets-add <slug> --profile newsom-mm
uv run polymaker markets-add <slug> --profile political-hot
```

## Step 5 — Validate strategy with the backtester

**Before going live with real money**, always validate with the backtester
using recorded journal data. The replay engine simulates fills against the
live tape, so you get a realistic estimate of:

- Realized spread PnL
- Liquidity reward accrual
- Maker rebate estimate
- Adverse selection (markout)
- Inventory drift

```bash
# Backtest against a recorded paper journal
uv run python scripts/backtest.py \
  --journal livecfg/logs/paper.jsonl \
  --profile political-longdated \
  --out-dir backtest_out/

# Backtest a synthetic quiet → jump → recovery tape
uv run python scripts/synth_regime_journal.py --dense \
  --out fixtures/regime_dense.jsonl
uv run python scripts/backtest.py \
  --journal fixtures/regime_dense.jsonl \
  --profile political-longdated \
  --out-dir backtest_out/
```

The backtester writes `backtest_out/backtest_summary.json` with per-market
PnL estimates. If the numbers are negative or the markout is heavily
negative, do NOT go live — adjust your strategy profile or pick different
markets.

## Step 6 — Paper trade with fill simulation

Paper mode now simulates fills. Run it against the live tape:

```bash
uv run polymaker run --paper
```

In paper mode:
- The bot runs the full market-WS → strategy → reconcile pipeline
- Trade prints from the live tape fill your resting orders (if they cross)
- Inventory, PnL, and toxicity (markout) are tracked exactly as in live
- No orders are posted to the exchange
- No user WS, no heartbeat loop

Watch the structured JSON log:

```bash
tail -f livecfg/logs/paper.jsonl
```

Key fields to monitor:
- `fill` events: simulated fills with price, size, side
- `mark` events: FV, regime, inventory
- `requote` events: place/cancel counts, regime, tox, flowz

## Step 7 — Preflight before going live

```bash
# Wallet auth + balances
uv run polymaker doctor

# Live order round-trip (~$5, refunds on cancel)
uv run polymaker livetest --notional 5

# Live taker round-trip (spends a little, use carefully)
uv run polymaker moneydoctor
```

If any of these fail, fix them before proceeding. See `TIPS.md` for
common failure modes (wrong signature type, stale clock, RPC issues).

## Step 8 — Go live (small size first)

```bash
# ONE process only — verify with pgrep
uv run polymaker run
```

Monitor continuously on day 1. The exchange heartbeat dead-man switch
cancels all orders within ~10s if the engine dies, but that is a safety
net, not a reason to leave it unwatched on a thin market.

## Step 9 — Unattended operation

For unattended operation, configure the alert webhook in `.env`:

```bash
ALERT_WEBHOOK_URL=https://ntfy.sh/your-secret-channel
```

The bot will alert on:
- API/auth failures
- Daily-loss kill
- WS disconnects (market, user, heartbeat)
- Process crashes
- Position divergence from on-chain
- Heartbeat recovery

For fully unattended cycles, follow `AUTONOMOUS_LOOP_PROTOCOL.md` and
`docs/STRATEGY_AGENT_TOOLING.md`.

## Category-specific strategy profiles

The shipped profiles in `config/strategy.toml` are tuned for specific
market archetypes:

| Profile              | Best for                              | Key knobs |
|----------------------|---------------------------------------|-----------|
| `newsom-mm`          | Deep, slow political markets          | Tight spread, low base size |
| `political-longdated`| General politics (>30d to expiry)     | Medium size, balanced skew |
| `political-hot`      | Short-dated, high-volume politics     | Smaller size, tighter spread |
| `romania-pm`         | Thin near-50/50 markets (reward min)  | Minimum reward size, sticky |
| `live-tiny`          | Defensive self-test (livecfg only)    | Tiny everything |

For new categories (sports, crypto, news), start with `political-longdated`
and tune per-market via `markets.toml` overrides. Always validate with
the backtester first.

## Daily operations checklist

- [ ] Check `WEEKLY_REPORT.md` for uptime + Tier-1 done count
- [ ] Verify exactly ONE engine process: `pgrep -f "polymaker run"`
- [ ] Check `logs/outage_status.json` for outage alerts
- [ ] Review `backtest_out/` for any strategy drift
- [ ] Confirm `ALERT_WEBHOOK_URL` is reachable (fire-drill monthly)

## When NOT to go live

- `scripts/paper_data_gate.py` reports `tier2_allowed=false`
- `recovery_smoke` fails after an outage
- Backtester shows negative PnL on your target markets
- Your `.env` is not configured (see `ESCALATE.md` entry 2)
- Alerts are not configured (see `ESCALATE.md` entry 4)
- You have not validated against ≥24h of paper runtime

## Key references

- [README.md](README.md) — overview + install
- [CLAUDE.md](CLAUDE.md) — agent guide, architecture, kill switches
- [TIPS.md](TIPS.md) — operator field notes (learn from a live session)
- [docs/strategy.md](docs/strategy.md) — strategy math (FV, skew, regime)
- [docs/market-selection.md](docs/market-selection.md) — scanner/scoring
- [docs/order-reconciliation.md](docs/order-reconciliation.md) — churn control
- [AUTONOMOUS_LOOP_PROTOCOL.md](AUTONOMOUS_LOOP_PROTOCOL.md) — unattended protocol
- [ESCALATE.md](ESCALATE.md) — known issues + escalation log
