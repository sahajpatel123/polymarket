# CLAIM.md — Agent Status Handoff

> **What this is:** A handoff document for AI agents (Claude, ChatGPT, Copilot, etc.)
> working on this codebase. Read this first before making any changes.
>
> **Who this is for:** Any AI model entering this workspace. It summarizes the
> current state, what was recently changed, what is known broken, and what
> should not be touched.

---

## Project

`polymaker` v2 — async Python market-making bot for Polymarket CLOB V2.
Maker-only, post-only. Single UV-managed package.

Key files: `src/polymaker/engine.py` (orchestration), `config.py` (pydantic
models over TOML), `risk/manager.py` (circuit breakers).

---

## Recent Changes (this session)

Three high-priority fixes applied:

### 1. Stale-quote window removed
**File:** `src/polymaker/engine.py` line ~1915

Before: On WS disconnect, the engine waited `ws_stale_halt_s` (10s) before
declaring the market stale and halting quoting. During that window, the quoter
could fire on frozen book data.

After: Market quoting halts **immediately** when the WS drops
(`not self.md.connected`). The `disconnected_since > 0.0` guard prevents false
positives during initial startup before first connection.

### 2. Concentration cap silent override removed
**File:** `src/polymaker/config.py` lines 143-148

Before: `resolve_from_bankroll()` computed `min(market_notional_frac * bankroll,
total_exposure * concentration_pct)` and overwrote `max_market_notional_usdc`
with the result. This made `market_notional_frac` useless whenever the
concentration cap was tighter.

After: `resolve_from_bankroll()` sets `max_market_notional_usdc` directly from
`market_notional_frac * bankroll`. The concentration cap is still enforced as a
separate check in `RiskManager.evaluate()` (`risk/manager.py:167-175`), so it
still fires — just without corrupting the market notional limit.

### 3. Per-market realized PnL tracking
**File:** `src/polymaker/risk/manager.py`

Before: `_market_pnl()` returned only unrealized mark-to-market drift. If a
market had realized -$50 in fill losses but zero remaining inventory, the
per-market kill switch would not trigger.

After: Tracks cumulative realized PnL from SELL fills per token in
`_per_token_realized_pnl`. `_market_pnl()` returns `realized + unrealized`.

---

## Test Results

**337 tests pass** with no regressions after the above changes.
Run with: `uv run pytest <test_file>...`

---

## What Is Known But NOT Fixed

These are callouts for any agent that picks up the next task:

### Compromised private key 🔴
The private key `0x72cd...2081` and wallet `0xC9E2...fe5` were exposed in plain
text in this chat session. **Must be rotated before any real funds deposited.**
The key is still in `.env` at `/Users/sahajpatel/Code/polymarket/.env`.

### 24h backtest results
See `backtest_24h_run/results/` for:
- `combined_journal.jsonl` (47.7MB, 66,652 events, two markets, ~12h trading)
- Per-market metrics (Newsom: 18 quotes, 0 fills; Vance: 40 quotes, 0 fills)
- All 58 quotes landed in reward band (100%).
- **0 fills** — post-only orders sat off-touch in QUIET regime; fill simulator
  requires aggressive prints that cross our quotes.
- **Key finding: $30 bankroll is insufficient** for reward-eligible orders
  (need ≥200 shares = ~$100 minimum).

### Unused TOML knobs
- `end_date_taper_days` and `exit_urgency_s` on `StrategyProfile` are defined
  in the pydantic model but never used by the live engine path. Kept for TOML
  compat only.

### intelligence/ module
Most classes in `intelligence/` are used only in test files, not in production
code. The ones actually wired:
- `DecisionFramework` — used by engine hot path
- `GrokAgent`, `GovernedGrokAgent`, `LLMGovernance` — wired LLM stack
- `MarketDiscovery`, `OversightLoop` — LLM loops
- `AgentMemory`, `ProfileHistory`, `SelfImprover` — V3 lifecycle
- `SelfEvaluation` — lazy import in engine
- `KalmanMidPrice` — replay fv_calibration

The rest (`AdaptiveSpreadParams`, `SmartExecutor`, `PortfolioState`,
`RiskState`, `SignalProcessor`, `SizingParams`, etc.) have tests but no
production imports.

### Dashboard
`metrics/live_dashboard.py` exists and serves a localhost UI. The `control/`
directory has only `__pycache__` — not a dead import path.

### Env vars
All `.env.example` vars are wired via `os.environ.get()` reads in their
respective modules. `POLYMAKER_CAPITAL_USDC`, `XAI_MODEL`,
`POLYMAKER_DASHBOARD_ALLOW_REMOTE`, etc. are **not** dead — they are just not
in the `Secrets()` pydantic class (correctly so — they are config, not secrets).

---

## Next Steps (for the next agent)

1. **Rotate the compromised key** — generate new wallet, update `.env`, never
   expose again.
2. **Fund with $100+** — $30 cannot meet Polymarket's 200-share minimum for
   reward-eligible orders. At $5 base_size, orders are 20x too small for
   rewards.
3. **Live paper test** — requires a machine that can reach
   `wss://ws-subscriptions-clob.polymarket.com/ws/market` (blocked from this
   network). Run `uv run polymaker run --paper`.
4. **Consider removing dead intelligence exports** from `__init__.py` (cosmetic;
   source files with tests should stay).
5. **Day-rollover PnL reset** — `_day_start_equity` resets on `reset_day()`
   which is called once at engine start, not at calendar day boundaries. This is
   known and documented.
