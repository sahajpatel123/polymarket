"""Share-adjusted reward planning — the real dominator KPI.

Headline number is **share-adjusted expected reward USDC**, not monopoly pool.

    share_adjusted = rewards_daily_rate × estimated_maker_share × uptime

Monopoly (100% of pool) is retained only as a labeled diagnostic ceiling.

Pure functions: unit-testable without I/O. Reuses:
  - :func:`polymaker.benchmark.capital.decide_maker_reward_eligibility`
  - :func:`polymaker.strategy.edge.competition_share`
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from polymaker.benchmark.capital import (
    MakerRewardEligibility,
    decide_maker_reward_eligibility,
)
from polymaker.strategy.edge import competition_share


DEFAULT_UPTIME = 0.85          # fraction of day we stay in-band and size-eligible
DEFAULT_MAX_SHARE = 0.35       # hard cap — never plan as if we own the book
DEFAULT_N_MAKERS = 3.0


@dataclass(frozen=True)
class ShareAdjustedPlan:
    """One market × one bankroll: the planning unit of domination."""

    condition_id: str
    bankroll_usdc: float
    eligible: bool
    skip: bool
    skip_reason: str

    # Sizing
    quote_size_usdc: float          # floored two-sided-capable order notional
    rewards_min_size: float
    typical_price: float

    # Pool economics
    rewards_daily_rate: float       # full market pool $/day (monopoly ceiling rate)
    market_liquidity: float
    estimated_share_of_pool: float  # 0..max_share
    monopoly_diagnostic_usdc: float  # pool × uptime (NOT the plan target)
    share_adjusted_expected_usdc: float  # HEADLINE: pool × share × uptime
    selection_score: float          # rank key = share_adjusted (0 if skip)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapitalScenarioReport:
    """Tight vs sufficient bankroll scenarios for operator planning."""

    scenarios: tuple[ShareAdjustedPlan, ...]
    headline_kpi: str = "share_adjusted_expected_usdc"
    note: str = (
        "Monopoly is a diagnostic ceiling only. "
        "Dominate by raising estimated_share_of_pool × eligible uptime, "
        "not by equating realistic to monopoly."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline_kpi": self.headline_kpi,
            "note": self.note,
            "scenarios": [s.as_dict() for s in self.scenarios],
        }


def plan_share_adjusted(
    *,
    bankroll_usdc: float,
    rewards_daily_rate: float,
    rewards_min_size: float,
    market_liquidity: float = 10_000.0,
    typical_price: float = 0.5,
    exchange_min_shares: float = 5.0,
    layers: int = 1,
    reward_size_mult: float = 1.0,
    condition_id: str = "",
    uptime: float = DEFAULT_UPTIME,
    max_share: float = DEFAULT_MAX_SHARE,
    n_competing_makers: float = DEFAULT_N_MAKERS,
    competitor_quote_usdc: float | None = None,
) -> ShareAdjustedPlan:
    """Compute share-adjusted expected daily reward for one market/capital.

    If capital cannot fund reward-min two-sided cycle → skip, expected $0.
    Otherwise floor quote size and estimate competition share.
    """
    gate: MakerRewardEligibility = decide_maker_reward_eligibility(
        bankroll_usdc=bankroll_usdc,
        rewards_min_size=rewards_min_size,
        exchange_min_shares=exchange_min_shares,
        typical_price=typical_price,
        layers=layers,
        reward_size_mult=reward_size_mult,
    )
    pool = max(0.0, float(rewards_daily_rate))
    up = min(1.0, max(0.0, float(uptime)))
    monopoly = pool * up

    if gate.skip or not gate.eligible:
        return ShareAdjustedPlan(
            condition_id=condition_id,
            bankroll_usdc=float(bankroll_usdc),
            eligible=False,
            skip=True,
            skip_reason=gate.reason or "ineligible",
            quote_size_usdc=0.0,
            rewards_min_size=float(rewards_min_size),
            typical_price=float(typical_price),
            rewards_daily_rate=pool,
            market_liquidity=float(market_liquidity),
            estimated_share_of_pool=0.0,
            monopoly_diagnostic_usdc=monopoly,
            share_adjusted_expected_usdc=0.0,
            selection_score=0.0,
        )

    quote = max(0.0, float(gate.recommended_base_size_usdc))
    share = competition_share(
        our_quote_usdc=quote,
        market_liquidity=float(market_liquidity or 0.0),
        max_share=max_share,
        n_competing_makers=n_competing_makers,
        competitor_quote_usdc=competitor_quote_usdc,
    )
    share_adj = pool * share * up
    return ShareAdjustedPlan(
        condition_id=condition_id,
        bankroll_usdc=float(bankroll_usdc),
        eligible=True,
        skip=False,
        skip_reason="",
        quote_size_usdc=quote,
        rewards_min_size=float(rewards_min_size),
        typical_price=float(typical_price),
        rewards_daily_rate=pool,
        market_liquidity=float(market_liquidity),
        estimated_share_of_pool=share,
        monopoly_diagnostic_usdc=monopoly,
        share_adjusted_expected_usdc=share_adj,
        # Rank by share-adjusted $; tiny epsilon for stability
        selection_score=share_adj,
    )


def plan_capital_scenarios(
    *,
    rewards_daily_rate: float,
    rewards_min_size: float,
    market_liquidity: float,
    typical_price: float = 0.5,
    bankrolls: Sequence[float] = (30.0, 2000.0),
    condition_id: str = "",
    **kwargs: Any,
) -> CapitalScenarioReport:
    """Run plan_share_adjusted for each bankroll (tight vs sufficient typical)."""
    plans = tuple(
        plan_share_adjusted(
            bankroll_usdc=float(b),
            rewards_daily_rate=rewards_daily_rate,
            rewards_min_size=rewards_min_size,
            market_liquidity=market_liquidity,
            typical_price=typical_price,
            condition_id=condition_id,
            **kwargs,
        )
        for b in bankrolls
    )
    return CapitalScenarioReport(scenarios=plans)


def rank_markets_by_share_adjusted(
    markets: Sequence[dict[str, Any]],
    *,
    bankroll_usdc: float,
    uptime: float = DEFAULT_UPTIME,
    max_share: float = DEFAULT_MAX_SHARE,
) -> list[ShareAdjustedPlan]:
    """Rank markets by share-adjusted expected USDC at fixed capital.

    Each market dict should include (at least):
      condition_id, rewards_daily_rate, rewards_min_size, liquidity_num
    Optional: typical_price, min_order_size, competitor_quote_usdc, n_makers.
    """
    plans: list[ShareAdjustedPlan] = []
    for m in markets:
        plans.append(
            plan_share_adjusted(
                bankroll_usdc=bankroll_usdc,
                rewards_daily_rate=float(m.get("rewards_daily_rate") or 0.0),
                rewards_min_size=float(m.get("rewards_min_size") or 0.0),
                market_liquidity=float(
                    m.get("liquidity_num") or m.get("market_liquidity") or 0.0
                ),
                typical_price=float(m.get("typical_price") or m.get("mid") or 0.5),
                exchange_min_shares=float(m.get("min_order_size") or 5.0),
                condition_id=str(m.get("condition_id") or ""),
                uptime=uptime,
                max_share=max_share,
                n_competing_makers=float(m.get("n_makers") or DEFAULT_N_MAKERS),
                competitor_quote_usdc=(
                    float(m["competitor_quote_usdc"])
                    if m.get("competitor_quote_usdc") is not None
                    else None
                ),
            )
        )
    plans.sort(key=lambda p: p.selection_score, reverse=True)
    return plans


# ── Multi-market portfolio + capacity curve (the actual game) ─────────


@dataclass(frozen=True)
class PortfolioPick:
    """One market in an optimized multi-market book."""

    condition_id: str
    allocated_usdc: float
    plan: ShareAdjustedPlan
    efficiency: float  # share_adj $/day per $ allocated

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "allocated_usdc": round(self.allocated_usdc, 4),
            "efficiency": round(self.efficiency, 8),
            **self.plan.as_dict(),
        }


@dataclass(frozen=True)
class MultiMarketPortfolio:
    """Best multi-market book for a bankroll: max Σ share_adjusted under caps."""

    bankroll_usdc: float
    picks: tuple[PortfolioPick, ...]
    total_share_adjusted_usdc: float
    total_allocated_usdc: float
    unallocated_usdc: float
    n_markets: int
    daily_return_pct: float  # fraction: total_share_adj / bankroll
    max_concentration: float
    note: str = (
        "return_%/day ≈ total_share_adjusted / capital. "
        "As capital grows, % naturally declines when reward surface is finite."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bankroll_usdc": round(self.bankroll_usdc, 4),
            "n_markets": self.n_markets,
            "total_share_adjusted_usdc": round(self.total_share_adjusted_usdc, 6),
            "total_allocated_usdc": round(self.total_allocated_usdc, 4),
            "unallocated_usdc": round(self.unallocated_usdc, 4),
            "daily_return_pct": round(self.daily_return_pct, 8),
            "daily_return_pct_display": round(self.daily_return_pct * 100.0, 4),
            "max_concentration": self.max_concentration,
            "picks": [p.as_dict() for p in self.picks],
            "note": self.note,
            "headline_kpi": "total_share_adjusted_usdc",
        }


@dataclass(frozen=True)
class CapacityPoint:
    """One bankroll on the capacity curve."""

    bankroll_usdc: float
    total_share_adjusted_usdc: float
    daily_return_pct: float
    n_markets: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "bankroll_usdc": round(self.bankroll_usdc, 4),
            "total_share_adjusted_usdc": round(self.total_share_adjusted_usdc, 6),
            "daily_return_pct": round(self.daily_return_pct, 8),
            "daily_return_pct_display": round(self.daily_return_pct * 100.0, 4),
            "n_markets": self.n_markets,
        }


@dataclass(frozen=True)
class CapacityCurve:
    """How share-adj $ and %/day scale with capital (physics, not a bug)."""

    points: tuple[CapacityPoint, ...]
    peak_pct_bankroll: float
    peak_pct: float
    current_bankroll: float
    current_pct: float
    capital_outgrew_reward_surface: bool
    outgrew_reason: str
    note: str = (
        "High %/day is easier at small capital on thin books. "
        "Capital outgrowing reward pools is success + physics, not strategy failure."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "points": [p.as_dict() for p in self.points],
            "peak_pct_bankroll": round(self.peak_pct_bankroll, 4),
            "peak_pct": round(self.peak_pct, 8),
            "peak_pct_display": round(self.peak_pct * 100.0, 4),
            "current_bankroll": round(self.current_bankroll, 4),
            "current_pct": round(self.current_pct, 8),
            "current_pct_display": round(self.current_pct * 100.0, 4),
            "capital_outgrew_reward_surface": self.capital_outgrew_reward_surface,
            "outgrew_reason": self.outgrew_reason,
            "note": self.note,
        }


def _market_kwargs(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "rewards_daily_rate": float(m.get("rewards_daily_rate") or 0.0),
        "rewards_min_size": float(m.get("rewards_min_size") or 0.0),
        "market_liquidity": float(
            m.get("liquidity_num") or m.get("market_liquidity") or 0.0
        ),
        "typical_price": float(m.get("typical_price") or m.get("mid") or 0.5),
        "exchange_min_shares": float(m.get("min_order_size") or 5.0),
        "condition_id": str(m.get("condition_id") or ""),
        "n_competing_makers": float(m.get("n_makers") or DEFAULT_N_MAKERS),
        "competitor_quote_usdc": (
            float(m["competitor_quote_usdc"])
            if m.get("competitor_quote_usdc") is not None
            else None
        ),
        "layers": int(m.get("layers") or 1),
        "reward_size_mult": float(m.get("reward_size_mult") or 1.0),
    }


def optimize_multi_market_portfolio(
    markets: Sequence[dict[str, Any]],
    *,
    bankroll_usdc: float,
    max_markets: int = 20,
    max_concentration: float = 0.40,
    uptime: float = DEFAULT_UPTIME,
    max_share: float = DEFAULT_MAX_SHARE,
) -> MultiMarketPortfolio:
    """Greedy multi-market pick: maximize Σ share_adjusted under bankroll.

    Algorithm (efficient, pure, no I/O):
      1. No hard cap on how many markets *can* be considered — only
         ``max_markets`` simultaneous slots and concentration per market.
      2. Repeatedly assign the next capital slice to the unpicked market with
         highest efficiency = share_adj_expected / allocated_usdc at that slice.
      3. Slice size = min(remaining, bankroll × max_concentration), subject to
         reward-min eligibility (skip if still ineligible).

    This is the “best multi-market book for this capital” path — not monopoly.
    """
    b = max(0.0, float(bankroll_usdc))
    if b <= 0 or not markets or max_markets <= 0:
        return MultiMarketPortfolio(
            bankroll_usdc=b,
            picks=(),
            total_share_adjusted_usdc=0.0,
            total_allocated_usdc=0.0,
            unallocated_usdc=b,
            n_markets=0,
            daily_return_pct=0.0,
            max_concentration=max_concentration,
        )

    remaining = b
    picks: list[PortfolioPick] = []
    used: set[str] = set()
    max_slice = max(1e-6, b * float(max_concentration))

    while remaining > 1e-6 and len(picks) < int(max_markets):
        best: PortfolioPick | None = None
        best_eff = -1.0
        for m in markets:
            cid = str(m.get("condition_id") or "")
            if not cid or cid in used:
                continue
            alloc = min(remaining, max_slice)
            if alloc <= 0:
                continue
            kw = _market_kwargs(m)
            plan = plan_share_adjusted(
                bankroll_usdc=alloc,
                uptime=uptime,
                max_share=max_share,
                **kw,
            )
            if plan.skip or plan.share_adjusted_expected_usdc <= 0:
                continue
            # Prefer markets that return more share-adj $ per dollar allocated
            eff = plan.share_adjusted_expected_usdc / alloc
            if eff > best_eff + 1e-15 or (
                abs(eff - best_eff) <= 1e-15
                and best is not None
                and plan.share_adjusted_expected_usdc
                > best.plan.share_adjusted_expected_usdc
            ):
                best_eff = eff
                best = PortfolioPick(
                    condition_id=cid,
                    allocated_usdc=alloc,
                    plan=plan,
                    efficiency=eff,
                )
        if best is None:
            break
        picks.append(best)
        used.add(best.condition_id)
        remaining -= best.allocated_usdc

    total_sa = sum(p.plan.share_adjusted_expected_usdc for p in picks)
    total_alloc = sum(p.allocated_usdc for p in picks)
    pct = (total_sa / b) if b > 0 else 0.0
    return MultiMarketPortfolio(
        bankroll_usdc=b,
        picks=tuple(picks),
        total_share_adjusted_usdc=total_sa,
        total_allocated_usdc=total_alloc,
        unallocated_usdc=max(0.0, remaining),
        n_markets=len(picks),
        daily_return_pct=pct,
        max_concentration=float(max_concentration),
    )


def capacity_curve(
    markets: Sequence[dict[str, Any]],
    *,
    bankrolls: Sequence[float] = (100.0, 200.0, 300.0, 500.0, 1000.0, 2000.0, 5000.0),
    current_bankroll: float | None = None,
    max_markets: int = 20,
    max_concentration: float = 0.40,
    outgrew_frac_of_peak: float = 0.50,
) -> CapacityCurve:
    """Build capacity curve and diagnose capital_outgrew_reward_surface.

    ``capital_outgrew_reward_surface`` is True when current bankroll's %/day
    is below ``outgrew_frac_of_peak`` × peak %/day *and* current bankroll is
    larger than the bankroll that achieved the peak %. That is the honest
    signal: capital succeeded past the dense part of the reward surface.
    """
    points: list[CapacityPoint] = []
    for b in bankrolls:
        port = optimize_multi_market_portfolio(
            markets,
            bankroll_usdc=float(b),
            max_markets=max_markets,
            max_concentration=max_concentration,
        )
        points.append(
            CapacityPoint(
                bankroll_usdc=float(b),
                total_share_adjusted_usdc=port.total_share_adjusted_usdc,
                daily_return_pct=port.daily_return_pct,
                n_markets=port.n_markets,
            )
        )

    if not points:
        return CapacityCurve(
            points=(),
            peak_pct_bankroll=0.0,
            peak_pct=0.0,
            current_bankroll=float(current_bankroll or 0.0),
            current_pct=0.0,
            capital_outgrew_reward_surface=False,
            outgrew_reason="no_points",
        )

    peak = max(points, key=lambda p: p.daily_return_pct)
    cur_b = float(
        current_bankroll if current_bankroll is not None else points[-1].bankroll_usdc
    )
    # Interpolate current from nearest portfolio solve (exact re-solve)
    cur_port = optimize_multi_market_portfolio(
        markets,
        bankroll_usdc=cur_b,
        max_markets=max_markets,
        max_concentration=max_concentration,
    )
    cur_pct = cur_port.daily_return_pct

    outgrew = False
    reason = "ok"
    if peak.daily_return_pct > 1e-12 and cur_b > peak.bankroll_usdc * 1.05:
        if cur_pct < peak.daily_return_pct * float(outgrew_frac_of_peak):
            outgrew = True
            reason = (
                f"current_pct={cur_pct:.4%} < {outgrew_frac_of_peak:.0%} of "
                f"peak_pct={peak.daily_return_pct:.4%} at bankroll={peak.bankroll_usdc:.0f}; "
                f"capital grew to {cur_b:.0f} past dense reward surface"
            )

    return CapacityCurve(
        points=tuple(points),
        peak_pct_bankroll=peak.bankroll_usdc,
        peak_pct=peak.daily_return_pct,
        current_bankroll=cur_b,
        current_pct=cur_pct,
        capital_outgrew_reward_surface=outgrew,
        outgrew_reason=reason,
    )
