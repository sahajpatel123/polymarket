"""Versioned prompt templates for Polymaker V3 LLM tools.

PROMPT_VERSION bumps when return schemas change. Each builder documents
when it is used and the JSON/tool format the model must return.
"""

from __future__ import annotations

from typing import Any

PROMPT_VERSION = "v3.1.0"

# ── Shared system preamble ─────────────────────────────────────────────

SYSTEM_PREAMBLE = """You are the advisory brain for Polymaker, a maker-only market-making bot on Polymarket.
You NEVER place orders, move funds, or bypass risk. Math quotes; risk enforces; you advise.
Always use the provided tools for structured actions. Free-form text is narrative only.
If you learn something durable, emit a line starting with MEMORY: [kind] optional@market_id text
Kinds: insight | finding | preference | rule.
Be concise, quantitative, and honest about uncertainty.
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
    user = f"""10-minute oversight snapshot. Analyze and propose bounded actions.

You are the trading brain controlling sizing, aggression, and capital rotation.
The math engine handles quoting and risk. You decide HOW MUCH and HOW AGGRESSIVE.

Guidelines for action types:
- size_up <cid>: increase position size by mult (1.3x = 30% larger). Use when:
  reward rate is high AND toxicity is low AND spread is farmable
- size_down <cid>: decrease position size. Use when toxicity spiking or
  resting_notional below reward_min
- go_aggressive <cid>: push band position UP (toward where trades happen) AND
  increase aggression 1.3x. Use when fill rate is low but market is safe.
- go_defensive <cid>: pull band position DOWN AND reduce aggression 0.7x.
  Use when toxicity > 0.2 or vol_ratio > 5 or flow_z > 2.
- pause_market <cid>: halt all quoting on this market. Use when toxicity > 0.3
  or flow_z > 3 or a clear adverse selection event is likely.
- tighten_spread <cid>: reduce spread multiplier (more competitive).
  Use on winning markets with positive daily trend.
- widen_spread <cid>: increase spread multiplier (safer). Use when drawdown
  > 5% or across all markets when daily loss is mounting.
- rotate_capital from=<src> to=<dst> amount=<usdc>: move capital from
  underperforming market to high-performer. Use when one market earns
  3x+ more than another per dollar deployed.
- no_op: nothing to change. Use sparingly — prefer action when you see
  a clear signal.

Dry-run guide: use dry_run=true ONLY when completely uncertain. Otherwise
dry_run=false — you have real authority to change sizing and aggression.

NEVER propose: side changes, directional bets, risk cap modifications.
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
