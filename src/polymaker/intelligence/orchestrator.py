"""Single-capital orchestrator.

Takes ``POLYMAKER_CAPITAL_USDC`` plus a list of candidate markets
(each with LLM confidence + expected reward) and produces the
final per-market allocation the engine should activate.

The orchestrator is the *only* place that decides "how much capital
goes where". Everything downstream (risk manager, quoting, sizing)
reads from the resulting :class:`AllocationPlan` and does not re-derive
arithmetic.

Design:
- Pure function: same inputs → same outputs (modulo dataclass id).
- No I/O. No LLM calls. No logging. The caller wires side effects.
- Greedy by default. We sort candidates by expected reward × confidence
  and fill the budget top-down. The first market that exceeds the
  per-market cap is *clamped*, not skipped — partial allocation is
  better than dropping a good market entirely.
- The total allocated is bounded by ``total_exposure_pct`` of capital.
- Allocations below the minimum-viable order size are zeroed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from polymaker.intelligence.policy import ResolvedPolicy
from polymaker.intelligence.sizing import allocation_from_confidence


@dataclass(frozen=True)
class MarketCandidate:
    """One market being considered for activation."""

    condition_id: str
    slug: str
    confidence: float                              # 0-1 from LLM
    expected_reward_per_day_usdc: float            # from LLM or estimator
    exchange_min_shares: float = 5.0
    reward_min_shares: float = 200.0
    typical_price: float = 0.5
    category: str = ""
    narrative: str = ""
    suggested_size_pct: float | None = None


@dataclass(frozen=True)
class MarketAllocation:
    """A single market's slice of the budget."""

    condition_id: str
    slug: str
    allocation_usdc: float
    confidence: float
    expected_reward_per_day_usdc: float
    rank: int
    skipped_reason: str = ""
    narrative: str = ""


@dataclass(frozen=True)
class AllocationPlan:
    """The full output of the orchestrator."""

    capital_usdc: float
    total_allocated_usdc: float
    unallocated_usdc: float
    allocations: tuple[MarketAllocation, ...]
    skipped: tuple[MarketAllocation, ...]
    policy_snapshot: ResolvedPolicy

    def active_markets(self) -> tuple[str, ...]:
        return tuple(a.condition_id for a in self.allocations)

    def for_market(self, condition_id: str) -> MarketAllocation | None:
        for a in self.allocations:
            if a.condition_id == condition_id:
                return a
        return None


def plan_allocations(
    candidates: Sequence[MarketCandidate],
    policy: ResolvedPolicy,
    *,
    min_viable_allocation_usdc: float = 5.0,
) -> AllocationPlan:
    """Build the allocation plan from candidates and a resolved policy."""
    if policy.capital_usdc <= 0:
        return AllocationPlan(
            capital_usdc=policy.capital_usdc,
            total_allocated_usdc=0.0,
            unallocated_usdc=0.0,
            allocations=(),
            skipped=(),
            policy_snapshot=policy,
        )

    max_per_market = policy.max_per_market_usdc
    total_budget = policy.total_exposure_usdc
    min_reward_floor = policy.min_reward_per_day_usdc

    viable: list[MarketCandidate] = []
    skipped: list[MarketAllocation] = []
    for c in candidates:
        if c.confidence <= 0 or c.expected_reward_per_day_usdc <= 0:
            skipped.append(MarketAllocation(
                condition_id=c.condition_id, slug=c.slug,
                allocation_usdc=0.0, confidence=c.confidence,
                expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
                rank=0, skipped_reason="zero_confidence_or_reward",
                narrative=c.narrative,
            ))
            continue
        if c.expected_reward_per_day_usdc < min_reward_floor:
            skipped.append(MarketAllocation(
                condition_id=c.condition_id, slug=c.slug,
                allocation_usdc=0.0, confidence=c.confidence,
                expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
                rank=0, skipped_reason="below_reward_floor",
                narrative=c.narrative,
            ))
            continue
        viable.append(c)

    viable.sort(
        key=lambda c: c.confidence * c.expected_reward_per_day_usdc,
        reverse=True,
    )

    remaining = total_budget
    accepted: list[MarketAllocation] = []
    for rank, c in enumerate(viable, start=1):
        if len(accepted) >= policy.policy.max_concurrent_markets:
            skipped.append(MarketAllocation(
                condition_id=c.condition_id, slug=c.slug,
                allocation_usdc=0.0, confidence=c.confidence,
                expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
                rank=rank, skipped_reason="max_markets_reached",
                narrative=c.narrative,
            ))
            continue
        if remaining <= 0:
            skipped.append(MarketAllocation(
                condition_id=c.condition_id, slug=c.slug,
                allocation_usdc=0.0, confidence=c.confidence,
                expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
                rank=rank, skipped_reason="budget_exhausted",
                narrative=c.narrative,
            ))
            continue

        per_market_cap = max_per_market
        if c.suggested_size_pct is not None and 0 < c.suggested_size_pct < 1.0:
            per_market_cap = min(per_market_cap, policy.capital_usdc * c.suggested_size_pct)

        raw = allocation_from_confidence(
            capital_usdc=policy.capital_usdc,
            confidence=c.confidence,
            expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
            max_per_market_pct=per_market_cap / policy.capital_usdc,
            min_reward_pct_per_day=policy.policy.min_reward_pct_per_day,
        )
        if raw < min_viable_allocation_usdc:
            skipped.append(MarketAllocation(
                condition_id=c.condition_id, slug=c.slug,
                allocation_usdc=0.0, confidence=c.confidence,
                expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
                rank=rank, skipped_reason="below_min_viable_size",
                narrative=c.narrative,
            ))
            continue

        allocation = min(raw, remaining, per_market_cap)
        accepted.append(MarketAllocation(
            condition_id=c.condition_id, slug=c.slug,
            allocation_usdc=allocation,
            confidence=c.confidence,
            expected_reward_per_day_usdc=c.expected_reward_per_day_usdc,
            rank=rank,
            narrative=c.narrative,
        ))
        remaining -= allocation

    total_allocated = sum(a.allocation_usdc for a in accepted)
    return AllocationPlan(
        capital_usdc=policy.capital_usdc,
        total_allocated_usdc=total_allocated,
        unallocated_usdc=max(0.0, total_budget - total_allocated),
        allocations=tuple(accepted),
        skipped=tuple(skipped),
        policy_snapshot=policy,
    )
