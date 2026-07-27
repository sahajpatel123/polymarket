# Polymaker V3 — Self-Automated AI Market Maker

> **Goal:** A single, intelligent market-making bot where you set *one* number (capital).
> The bot auto-discovers markets, decides *what* to trade, *how much*, *when to stop*,
> *how to recover from loss*, and continuously improves itself via an integrated LLM
> (Grok 4.5 by xAI).

---

## 0. Honest Scope Note (read first)

- There is **no target growth, no profit cap, no "you've made enough"**.
  The bot earns as much as it can, subject only to the loss-side caps
  (daily loss kill, drawdown kill, per-market loss, per-trade loss).
  Every cap is a percentage of *current* capital, so the bot can
  compound without a ceiling.
- The user's aspirational daily range of 10–13% is logged and
  compared against realized PnL for visibility, but the system has no
  mechanism to "stop at 13%". A 50% day is just as welcome as a
  10% day.
- LLM is **not magic** — it gives better context (news, narrative, regime), not
  alpha. The mathematical edge (microprice, inventory skew, regime) still does the
  actual quoting. LLM suggests, math decides, risk enforces.
- All caps are fractions of capital. The system is honest about what
  it can extract: even with an LLM, the realistic edge is closer to
  0.5–4%/day on a working book. The system tracks realized vs.
  aspirational but never overpromises in code.

---

## 1. The Single User Knob

```env
# .env
PK=...                          # signer key (existing)
BROWSER_ADDRESS=...             # funder (existing)
XAI_API_KEY=...                 # NEW — Grok 4.5 API key
POLYMAKER_CAPITAL_USDC=500      # NEW — the ONLY config you touch
```

That's it. Everything else (markets, sizes, risk, regimes, timeouts) is derived
from `POLYMAKER_CAPITAL_USDC` + live market state + LLM context.

Optional overrides (rarely needed):
```env
POLYMAKER_RISK_PROFILE=balanced  # conservative | balanced | aggressive
POLYMAKER_MAX_MARKETS=8          # hard cap on concurrent markets
# Note: there is NO target/profit cap. The bot earns without ceiling.
```

---

## 2. Architecture

```
                ┌────────────────────────────────────┐
                │  GROK 4.5 AGENT (xAI)              │
                │  - Market selection (rank)         │
                │  - News / narrative context        │
                │  - Regime commentary / override    │
                │  - Self-improvement suggestions    │
                │  - End-of-day review               │
                └─────────────┬──────────────────────┘
                              │  JSON tool calls
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │                   ORCHESTRATOR                           │
   │  - Single capital knob → allocation across markets        │
   │  - %-based sizing (per-market, per-trade, per-loss)       │
   │  - %-based targets (daily PnL, max loss, max drawdown)    │
   │  - Improvement loop: read self_eval, ask LLM, apply      │
   └────────┬───────────────┬───────────────┬────────────────┘
            │               │               │
            ▼               ▼               ▼
       STRATEGY          EXECUTION         RISK
       (pure math)       (gateway)         (manager)
            │               ▲               ▲
            ▼               │               │
       MARKETDATA ────  Engine  ────  State / SQLite
            ▲
            │
       GAMMA (auto-discover every 10 min)
```

---

## 3. New Subsystems

### 3.1 `intelligence/agent.py` — Grok 4.5 Integration

A thin, **rate-limited**, **prompt-cached** xAI client with strict JSON tool-calling.

**When the agent is called (NOT every tick — that would be insane):**

| Trigger | What it does | Cost/cycle |
|---------|--------------|------------|
| Market selection cycle (every 10 min) | Ranks Gamma candidates, returns top N with `confidence` and `reason` | 1 call, ~1k tokens |
| Regime escalation (when math says TRENDING/TOXIC) | Asks "is this real news or noise?" Returns `narrative` + `spread_mult` | 1 call, ~0.5k |
| End-of-day review (UTC 23:55) | Reviews day's PnL, suggests profile tweaks for tomorrow | 1 call, ~2k |
| Manual `/improve` (CLI) | Ad-hoc analysis request | on demand |
| Continuous **commentary** (every 30 min) | Free-form: "I notice X, consider Y" — *advisory only* | 1 call, ~0.3k |

**What it does NOT do:**
- Place orders directly. Math does.
- Override a risk halt. Ever.
- See private keys. It sees market data + state.

**Cost ceiling:** configurable max tokens/day. Default: 50k tokens/day (~$0.10 with Grok 4.5 fast).

### 3.2 `intelligence/orchestrator.py` — Single Capital Allocator

Takes `POLYMAKER_CAPITAL_USDC` and a list of `(market, confidence, expected_reward_per_day)` tuples (some from LLM, some from math) and:

```
total_capital = POLYMAKER_CAPITAL_USDC
max_per_market = min(
    total_capital * 0.05,   # hard 5% per market (configurable)
    market.reward_pool / 10  # never dominate the pool
)
for m in markets_by_priority:
    allocation = min(
        m.suggested_pct * total_capital,  # LLM-weighted suggestion
        max_per_market,
        remaining_capital
    )
    if allocation < exchange_min_for_reward:
        skip()  # too small to earn — don't bother
    else:
        activate(m, allocation)
```

### 3.3 `intelligence/sizing.py` — %-Based Per-Trade Sizing

Replaces fixed `base_size_usdc` with:
- Per-trade max risk: `pct_of_allocation * 0.5%` (default, configurable)
- Per-trade max size: `allocation * 0.3` (never more than 30% of one side in one order)
- Hard loss-per-trade: `pct_of_allocation * loss_pct * fv_distance`
- Always rounds to exchange min and reward min

### 3.4 `intelligence/policy.py` — %-Based Risk Policy (loss-only)

Replaces absolute `daily_loss_kill_usdc` etc. with percentages.
There is **no** `target_*` field. The bot earns without ceiling.
```
daily_loss_kill_pct   = 0.10   # -10% of capital in a day = halt
max_drawdown_kill_pct = 0.25   # -25% from peak = halt + review
per_market_loss_pct   = 0.05   # -5% of capital in one market = reduce-only
per_trade_loss_pct    = 0.005  # -0.5% of capital = tight stop on a single fill
```

All computed from `POLYMAKER_CAPITAL_USDC` at boot and on hot-reload.

### 3.5 `intelligence/discovery.py` — Auto-Discover (LLM-Ranked)

Replaces manual `markets.toml`. Every 10 min:
1. Scan Gamma for markets with `rewardsMinSize <= 200` (reward-eligible)
2. Filter: enough liquidity, not closed, not in our `halted` set
3. Send top 30 to LLM with: reward rate, daily volume, bid/ask spread, our recent edge estimate
4. LLM returns ranked list with `confidence` (0-1) and `narrative`
5. Activate top N where `confidence * reward_per_day > threshold`

`markets.toml` becomes a **watchlist of allowed categories** (or just removed entirely if you trust the LLM).

### 3.6 `intelligence/self_improve.py` — The Improvement Loop

Every cycle, the system:
1. Computes `SelfEvaluation` (already exists, v6)
2. If `is_decaying()` → pause, call LLM with: "Strategy decaying, here's last 50 trades, what to change?"
3. LLM returns a `Suggestion` (one of: tighten spread, widen spread, drop market, change regime threshold, no action)
4. Suggestions are **applied to a draft profile**, then **paper-validated for 1 hour** before going live
5. After 24h, end-of-day review LLM call suggests next-day profile baseline

This is the "make changes if needed" part the user asked for — but **gated through paper validation**, never live-edited.

### 3.7 `intelligence/oversight.py` — Continuous Commentary

Every 30 min the LLM gets a snapshot:
- Current PnL, drawdown, fill rate, top-of-book state
- Any anomalies (sudden spread widening, inventory build-up, regime flips)

Returns a `Commentary` object: `narrative + optional action` (action types: `tighten_spread`, `widen_spread`, `pause_market`, `add_layer`, `drop_market`). All actions go through risk manager.

---

## 4. Tool-Calling Contract (LLM ↔ Math)

Grok 4.5 only ever sees this JSON tool schema. It cannot place orders, cannot move funds, cannot bypass risk.

```json
{
  "tools": [
    {"name": "rank_markets", "params": {"candidates": [...], "top_n": 5}},
    {"name": "comment_on_regime", "params": {"market": "...", "math_says": "TRENDING"}},
    {"name": "suggest_improvement", "params": {"metrics": {...}}},
    {"name": "review_day", "params": {"pnl_summary": {...}}}
  ]
}
```

Each tool returns structured JSON, not free text. Free text is `narrative` only.

---

## 5. Config Surface (after this change)

**.env (you set these):**
```
PK, BROWSER_ADDRESS, XAI_API_KEY, POLYMAKER_CAPITAL_USDC
```

**`config/policy.toml` (new, single file for percentages):**
```toml
[policy]
max_per_market_pct = 0.05
daily_loss_kill_pct = 0.10
max_drawdown_kill_pct = 0.25
per_market_loss_pct = 0.05
per_trade_loss_pct = 0.005
risk_profile = "balanced"
max_concurrent_markets = 8
min_reward_pct_per_day = 0.005   # skip markets with less than 0.5%/day expected
# NO target_growth field. The bot earns without ceiling.
```

**`config/strategy.toml`** stays for **baseline profile shape** (spread math, regime thresholds). Bot can override within %-bounds from policy.

**`config/markets.toml`** becomes optional `watchlist` of allowed slugs/tags, OR is removed entirely. Default: removed, all auto-discovery.

---

## 6. CLI Surface (new commands)

```bash
polymaker run                       # auto-pilot: set capital, go
polymaker run --paper               # same, paper mode
polymaker status                    # what am I doing right now?
polymaker pnl                       # %-based PnL view
polymaker improve                   # ask LLM for improvement suggestions
polymaker review                    # end-of-day-style review
polymaker explain <condition_id>    # why am I quoting this? (LLM narrative)
polymaker capital                   # show capital allocation breakdown
```

---

## 7. Testing Strategy

- Unit tests for: orchestrator allocation, sizing math, policy %, LLM mock client
- Integration: paper run for 2h with LLM mocked, verify cycle runs and no crashes
- Cost test: assert < 50k tokens/day under default triggers
- Safety test: LLM **cannot** bypass risk (test with malicious tool returns)
- Backwards-compat: existing `markets.toml` still works (watchlist mode)

---

## 8. Implementation Phases

**Phase 1 — Single-capital refactor (1-2 days)**
- `policy.py` with %-based risk
- `orchestrator.py` with capital allocation
- `sizing.py` with %-based per-trade
- Update `config.py` to read `POLYMAKER_CAPITAL_USDC`
- `markets.toml` becomes optional watchlist

**Phase 2 — Grok 4.5 agent (1-2 days)**
- `agent.py` xAI client with rate limits + cost cap
- `oversight.py` continuous commentary
- `discovery.py` LLM-ranked market selection

**Phase 3 — Self-improvement loop (1-2 days)**
- `self_improve.py` with paper-validation gate
- End-of-day review
- CLI: `improve`, `review`, `explain`

**Phase 4 — Tests + paper validation (1 day)**
- Unit + integration + safety tests
- 2h paper run with mocked LLM
- 2h paper run with real LLM (small token budget)

**Total: ~1 week of code, fully paper-validated before any real money.**

---

## 9. What This Does NOT Do (honesty)

- Does not cap profits. There is no "you've made enough" threshold.
  A 50% day is welcome. The user's 10-13% aspirational range is
  logged but not enforced.
- Does not guarantee 10-13% daily. It will try, measure, and report honestly.
- Does not replace the math. Math quotes; LLM suggests.
- Does not trade on news alone. LLM is a *narrative* input to the math, not a signal.
- Does not let the LLM touch wallets, keys, or risk halts. Ever.
- Does not silently scale. Every size change passes through risk + (for profile changes) paper validation.

---

## 10. The One Promise This System Makes

If you set `POLYMAKER_CAPITAL_USDC=500`, the bot will:
- **Within 1 hour** be live on the best markets it can find
- **Every 10 min** re-evaluate whether to add/drop markets
- **Every 30 min** narrate to you what it's doing and why
- **Every day** show you exactly what it made, what it lost, and what it wants to change
- **On every bad day** automatically reduce risk, then ask the LLM why, then tell you
- **Earn as much as it can** without a profit cap, only loss-side stops
- **Never** risk more than the % caps you set
- **Never** silently change its own risk policy

That's the actual contract. No profit ceiling. No fixed-target
logic. The bot compounds as much as the markets allow, gated only
by the loss caps.

---

## Open Questions Before Implementation

1. **Token cap default: 50k/day — OK or higher?** Grok 4.5 fast is cheap, but if you want continuous commentary every 5 min instead of 30, this becomes 6× cost.
2. **Watchlist vs full auto-discovery:** should `markets.toml` still constrain to categories (e.g. only politics) or fully open? Default: full open, configurable.
3. **LLM model:** Grok 4.5 has multiple variants (fast, standard, reasoning). Default: Grok 4.5 fast for selection/commentary, Grok 4.5 reasoning for end-of-day review.
4. **Profile override rules:** if the bot wants to widen spread by 20%, that's fine; if it wants to change daily_loss_kill, that's vetoed. Confirm this gating.
5. **Self-improvement paper gate duration:** 1 hour default. OK?

Once these are confirmed, Phase 1 can start.
