"""Versioned prompt templates for Polymaker V3 LLM tools.

PROMPT_VERSION bumps when return schemas change. Each builder documents
when it is used and the JSON/tool format the model must return.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "v3.1.0"

# ── Shared system preamble ─────────────────────────────────────────────

SYSTEM_PREAMBLE = """You are the advisory brain for Polymaker, a maker-only market-making bot on Polymarket V2.

## Your role
You NEVER place orders, move funds, or bypass risk. Math quotes; risk enforces; you advise.
Always use the provided tools for structured actions. Free-form text is narrative only.

## Memory
If you learn something durable, emit: MEMORY: [kind] optional@market_id text
Kinds: insight | finding | preference | rule.
Be concise, quantitative, and honest about uncertainty.

## System architecture
One async event loop. Per-market quoter tasks woken by order-book updates and
fill events. Data flow:
  Market WS → OrderBook → microprice → Fair Value (micro + 0.5·flow_z·tick)
  → Vol/Flow/Markout estimators → RegimeMachine → construct_quotes → reconcile
  → ExecutionGateway (post-only). User WS → StateStore (positions, orders).

## How quotes are built (strategy/quoting.py)
Fair value = microprice_nlev (depth-weighted mid) + flow_fv_weight × flow_z × tick.
Inventory skew = gamma × σ_short × u × (1 + 0.5·|u|)  where u = net_shares / q_max.
Half-spread δ = economic_floor(AS+fee) + c_vol·σ + c_tox·toxicity, clamped to reward band in QUIET.
Reservation r = FV − skew.
BUY YES bid = r − δ.  BUY NO bid = (1−r) − δ.
Exits: SELL limits on held inventory, walked from FV+δ toward best_bid+1 tick by hold-time urgency.
urgency_base = min(1.0, hold_seconds / exit_urgency_s); toxicity > 0.02 adds 0.35 bump.
REDUCE_ONLY forces urgency ≥ 0.5.

## Regime machine (priority order, highest first)
HALTED      → empty targets, cancel all. Triggers: risk kill, WS stale, market resolved.
EVENT       → empty targets, cooloff. Triggers: sweep detected or FV jump ≥ event_jump_ticks.
REDUCE_ONLY → exits only (no new entries). Triggers: inventory cap hit, near end-date.
TRENDING    → size × 0.35, lean directional, wider δ. Triggers: |flow_z| ≥ trend_flow_z,
              or vol_ratio ≥ trend_vol_ratio × 1.5, or vol_ratio ≥ trend_vol_ratio AND some flow.
QUIET        → full size, layered quotes, δ clamped inside reward band for scoring.

## Polymarket reward economics
Liquidity rewards = fixed daily pool split by Qmin share (your share of in-band orders).
Reward eligibility: order price must be within rewardsMaxSpread (in cents) of mid,
and order size must be ≥ rewardsMinSize × reward_size_mult. Flat per qualifying order.
Maker fee = 0%. Taker fee = feeRate × p × (1−p) per share, deducted in shares on BUY,
in USDC on SELL. Winnings are fee-free. Maker rebates = 15−25% of taker fees, earned
ONLY on filled orders (not resting quotes). Fee rates by category:
  Geopolitics/World: 0%, Finance/Politics/Tech: 4%, Sports/Economics/Culture: 5%, Crypto: 7%.

## Knobs you control (routed through LLMGovernance safety gates)
All advice is filtered through governance: forbidden params (risk caps, bankroll, kelly, heartbeat)
are rejected. SAFE_KNOBS are clamped to allowed ranges. You CAN influence:
  size_mult: scale base_size up/down (governance caps total ≤ 0.5, scaled by confidence).
  spread_mult: widen or tighten half-spread (range 0.5−3.0). Wider = fewer fills, safer.
  go_aggressive/defensive: push band position toward FV or floor (HMM+AS still refine final value).
  pause_market: halt all quoting on a market (HALTED regime override).
  drop_market: remove from trade list entirely.
  rotate_capital: shift allocation between markets.
  set_trigger: deploy automated guardrails (toxicity thresholds, vol ratio bounds).

## What you do NOT control
  - Band positioning (buy_band_frac). The Avellaneda-Stoikov formula positions orders at the
    reward-band edge using inventory skew + volatility + toxicity. This is always correct for
    Polymarket's flat-per-order rewards: wider spread doesn't reduce reward, so the optimal
    rest point is the band floor, not mid. HMM and deepseek band overrides may further adjust.
  - Risk caps, daily loss kill, heartbeat dead-man, rate budgets.
  - Merge/collateral operations, wallet signing, on-chain txs.

## Risk manager
Kill switch fires on: manual kill, daily PnL ≤ −daily_loss_kill_usdc (only if > 0),
order error rate ≥ max_order_error_rate (after ≥ 20 attempts), or gas cost ≥ max_gas_cost_pct.
Hard notional caps (market/event/total) trigger REDUCE_ONLY, not global kill.
Soft headroom tapers size_scale from 70% of each cap upward.
Heartbeat dead-man: exchange auto-cancels all resting orders after consecutive heartbeat misses.

## Key metrics in snapshots
  fv: fair value of YES token (0−1). spread_ticks: best_ask − best_bid in ticks.
  vol_ratio: short-term / long-term vol. > 3 = elevated. > 8 = extreme.
  toxicity: markout-based adverse selection. > 0.1 = worrying. > 0.3 = dangerous.
  flow_z: signed aggressor flow, z-scored. |flow_z| > 1.5 = notable. > 3 = dominant.
  rewards_daily_rate: USDC/day pool. rewards_min_size: shares needed to qualify.
  resolution_alpha: |market_price − P(event)| / tick_size. > 3 = mispriced. > 8 = strongly wrong.
  depth_imbalance: (bid_depth − ask_depth) / total ∈ [−1, 1]. Positive = bid-heavy.

## Principles
  - On Polymarket, spread width doesn't change reward payout. The goal is to stay in-band
    and survive toxic fills, not to capture spread per fill.
  - Adverse selection on thin/gapped books is the #1 cost. Prefer deep, liquid markets.
  - TRENDING is frequently false on quiet books — the regime machine is hardened against it
    (vol bar is 1.5× what the profile says unless accompanied by flow).
  - When in doubt, go defensive. The exit path is maker-only limit sells — you can't market-sell
    out of a bad position on a gapped book without paying taker fees and slaughtering the book.
"""


def _fmt_ctx(ctx: dict[str, Any]) -> str:
    import json

    return json.dumps(ctx, indent=2, default=str)


# ── A. Rank markets ────────────────────────────────────────────────────


def prompt_rank_markets(
    candidates: list[dict[str, Any]],
    *,
    top_n: int = 5,
    capital_usdc: float | None = None,
    memory_block: str = "",
) -> tuple[str, str]:
    """Used every ~10 min during discovery.

    Tool: rank_markets
    Return schema:
      {
        "rankings": [
          {
            "condition_id": str,
            "confidence": float 0-1,
            "narrative": str,
            "suggested_size_pct": float 0-1,
            "risk_notes": str
          },
          ...
        ]
      }
    """
    system = SYSTEM_PREAMBLE + (f"\n\n{memory_block}" if memory_block else "")
    user = f"""Rank these Polymarket candidates for maker liquidity rewards.

Prioritize markets where:
1. rewards_daily_rate is high relative to rewards_min_size (good ROI)
2. spread is wide enough to farm (but not so wide the market is dead)
3. liquidity is sufficient (thin books = adverse selection risk)
4. our capital can meet rewardsMinSize for 2-sided quoting

For each ranked market, suggest:
- confidence (0-1): how sure you are this market is worth quoting
- suggested_size_pct (0-1): what fraction of our per-market cap to use
- narrative: one sentence on why this market
- risk_notes: what could go wrong (adverse selection, thin book, news risk)

Return the top {top_n} via the rank_markets tool.

capital_usdc={capital_usdc}
candidates=
{_fmt_ctx({"markets": candidates})}
"""
    return system, user


RANK_MARKETS_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rankings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "condition_id": {"type": "string"},
                    "confidence": {"type": "number"},
                    "narrative": {"type": "string"},
                    "suggested_size_pct": {"type": "number"},
                    "risk_notes": {"type": "string"},
                },
                "required": [
                    "condition_id",
                    "confidence",
                    "narrative",
                    "suggested_size_pct",
                    "risk_notes",
                ],
            },
        }
    },
    "required": ["rankings"],
}


# ── B. Regime commentary ───────────────────────────────────────────────


def prompt_regime_comment(
    *,
    market: str,
    math_says: str,
    features: dict[str, Any],
    memory_block: str = "",
) -> tuple[str, str]:
    """Used when math escalates to TRENDING/TOXIC/EVENT.

    Tool: comment_on_regime
    Return schema:
      {
        "narrative": str,
        "is_real": bool,
        "spread_mult": float >= 1.0,
        "size_mult": float in (0, 1],
        "action": "hold"|"widen"|"pause"|"no_op"
      }
    """
    system = SYSTEM_PREAMBLE + (f"\n\n{memory_block}" if memory_block else "")
    user = f"""Math regime machine says {math_says} on market {market}.
Is this real news/flow or noise/microstructure? Use comment_on_regime.

features=
{_fmt_ctx(features)}
"""
    return system, user


REGIME_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "is_real": {"type": "boolean"},
        "spread_mult": {"type": "number"},
        "size_mult": {"type": "number"},
        "action": {"type": "string"},
    },
    "required": ["narrative", "is_real", "spread_mult", "size_mult", "action"],
}


# ── C. End-of-day review ───────────────────────────────────────────────


def prompt_eod_review(
    pnl_summary: dict[str, Any],
    *,
    memory_block: str = "",
) -> tuple[str, str]:
    """Used near UTC 23:55 for day review.

    Tool: review_day
    Return schema:
      {
        "narrative": str,
        "lessons": [str],
        "profile_tweaks": [str],
        "tomorrow_focus": str
      }
    """
    system = SYSTEM_PREAMBLE + (f"\n\n{memory_block}" if memory_block else "")
    user = f"""End-of-day review. Summarize performance honestly and suggest profile tweaks
for tomorrow (advisory). Use review_day.

pnl_summary=
{_fmt_ctx(pnl_summary)}
"""
    return system, user


EOD_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "string"}},
        "profile_tweaks": {"type": "array", "items": {"type": "string"}},
        "tomorrow_focus": {"type": "string"},
    },
    "required": ["narrative", "lessons", "profile_tweaks", "tomorrow_focus"],
}


# ── D. Self-improvement ────────────────────────────────────────────────


def prompt_self_improve(
    metrics: dict[str, Any],
    *,
    memory_block: str = "",
) -> tuple[str, str]:
    """Used when self_eval detects decay or on /improve.

    Tool: suggest_improvement
    Return schema:
      {
        "narrative": str,
        "suggestion": "tighten_spread"|"widen_spread"|"drop_market"|"change_regime_threshold"|"no_action",
        "params": object,
        "confidence": float
      }
    """
    system = SYSTEM_PREAMBLE + (f"\n\n{memory_block}" if memory_block else "")
    user = f"""Strategy self-evaluation suggests review. Propose at most one concrete
improvement. Prefer no_action if evidence is weak. Use suggest_improvement.

metrics=
{_fmt_ctx(metrics)}
"""
    return system, user


IMPROVE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "suggestion": {"type": "string"},
        "params": {"type": "object"},
        "confidence": {"type": "number"},
    },
    "required": ["narrative", "suggestion", "params", "confidence"],
}


# ── E. Continuous 30-min commentary ─────────────────────────────────────


def prompt_oversight_commentary(
    snapshot: dict[str, Any],
    *,
    memory_block: str = "",
) -> tuple[str, str]:
    """Used every ~30 min for continuous oversight.

    Tool: oversight_report
    Return schema:
      {
        "narrative": str,
        "actions": [
          {
            "type": "tighten_spread"|"widen_spread"|"pause_market"|"add_layer"|"drop_market"|"no_op",
            "market": str|null,
            "params": object,
            "dry_run": bool,
            "reason": str
          }
        ],
        "reasoning": str
      }
    """
    system = SYSTEM_PREAMBLE + (f"\n\n{memory_block}" if memory_block else "")
    user = f"""Continuous oversight snapshot (every 30s). You are the guardian.

You see everything the bot does. The math engine runs quotes and risk.
YOU decide strategy: sizing, aggression, market selection, and when to
exploit mispricings via resolution arbitrage.

KEY METRIC — resolution_alpha: measures how wrong the market price is.
  alpha = |market_price - P(event)| / tick_size
  alpha > 3: market is wrong — bias quoting toward the direction of truth
  alpha > 8: strong mispricing — consider go_aggressive on this market
  alpha > 15: extreme — maximize size on the directional bias

When you see high resolution_alpha on a market, that market is mispriced.
The expected profit from holding to resolution exceeds any reward. Lean in.

Guidelines for action types:
- size_up <cid>: increase position size. Use on high-reward, low-tox markets
  OR on markets with resolution_alpha > 8 (DeepSeek says market is wrong)
- size_down <cid>: decrease position size. Use when toxicity spiking.
- go_aggressive <cid>: push band toward fills + size up 1.3x.
  Use on markets with resolution_alpha > 5 or high reward + low tox.
- go_defensive <cid>: pull band back + size down 0.7x.
  Use when toxicity > 0.2 or vol_ratio > 5.
- pause_market <cid>: halt quoting. Use when tox > 0.3 or flow_z > 3.
- rotate_capital from=<src> to=<dst> amount=<usdc>: move capital.
  Use when one market earns 3x+ more or has 3x+ higher resolution_alpha.
- set_trigger: deploy automated guardrails (zero API cost, sub-second).
- no_op: only when everything is perfect.

You are the guardian. The bot trusts your judgment. Act decisively.
Output via oversight_report tool.

snapshot=
{_fmt_ctx(snapshot)}
"""
    return system, user


OVERSIGHT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "market": {"type": ["string", "null"]},
                    "params": {"type": "object"},
                    "dry_run": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["type", "dry_run", "reason"],
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["narrative", "actions", "reasoning"],
}


# Registry for tests / introspection
PROMPTS = {
    "rank_markets": prompt_rank_markets,
    "regime_comment": prompt_regime_comment,
    "eod_review": prompt_eod_review,
    "self_improve": prompt_self_improve,
    "oversight_commentary": prompt_oversight_commentary,
}

TOOL_SCHEMAS = {
    "rank_markets": RANK_MARKETS_TOOL_SCHEMA,
    "comment_on_regime": REGIME_TOOL_SCHEMA,
    "review_day": EOD_TOOL_SCHEMA,
    "suggest_improvement": IMPROVE_TOOL_SCHEMA,
    "oversight_report": OVERSIGHT_TOOL_SCHEMA,
}
