"""Share-adjusted reward planning — the real dominator KPI.

Headline number is **share-adjusted expected reward USDC**, not monopoly pool.

    share_adjusted = rewards_daily_rate × estimated_maker_share × uptime

Monopoly (100% of pool) is retained only as a labeled diagnostic ceiling.

Pure functions: unit-testable without I/O. Reuses:
  - :func:`polymaker.benchmark.capital.decide_maker_reward_eligibility`
  - :func:`polymaker.strategy.edge.competition_share`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

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


# AS haircut weight on portfolio efficiency (thin-book toxicity).
# Efficiency uses risk-adjusted share-adj so we don't "dominate" toxic books.
DEFAULT_AS_WEIGHT = 0.55


def as_risk_proxy(
    *,
    liquidity_num: float = 0.0,
    rewards_max_spread: float = 3.0,
    typical_price: float = 0.5,
) -> float:
    """0..1 adverse-selection proxy (aligned with catalog.scoring.adverse_selection_risk).

    Pure so portfolio can use market dicts without MarketMeta.
    """
    mid = min(max(float(typical_price), 0.01), 0.99)
    ext = min(1.0, abs(mid - 0.5) / 0.5)
    band = max(0.0, float(rewards_max_spread or 0.0))
    band_risk = min(1.0, band / 10.0)
    liq = max(0.0, float(liquidity_num or 0.0))
    thin = 1.0 - min(1.0, liq / 20000.0)
    return min(1.0, 0.45 * ext + 0.30 * band_risk + 0.25 * thin)


def risk_adjust_share_adj(
    share_adj_usdc: float,
    as_risk: float,
    *,
    as_weight: float = DEFAULT_AS_WEIGHT,
) -> float:
    """Haircut share-adjusted expected $ by AS risk (never increases it)."""
    w = min(1.0, max(0.0, float(as_weight)))
    r = min(1.0, max(0.0, float(as_risk)))
    return max(0.0, float(share_adj_usdc) * (1.0 - w * r))


@dataclass(frozen=True)
class PortfolioPick:
    """One market in an optimized multi-market book."""

    condition_id: str
    allocated_usdc: float
    plan: ShareAdjustedPlan
    efficiency: float  # risk-adjusted share_adj $/day per $ allocated
    as_risk: float = 0.0
    risk_adjusted_share_usdc: float = 0.0
    raw_efficiency: float = 0.0  # pre-AS haircut efficiency (diagnostic)

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "allocated_usdc": round(self.allocated_usdc, 4),
            "efficiency": round(self.efficiency, 8),
            "raw_efficiency": round(self.raw_efficiency, 8),
            "as_risk": round(self.as_risk, 4),
            "risk_adjusted_share_usdc": round(self.risk_adjusted_share_usdc, 6),
            **self.plan.as_dict(),
        }


@dataclass(frozen=True)
class MultiMarketPortfolio:
    """Best multi-market book for a bankroll: max Σ risk-adjusted share under caps."""

    bankroll_usdc: float
    picks: tuple[PortfolioPick, ...]
    total_share_adjusted_usdc: float
    total_risk_adjusted_usdc: float
    total_allocated_usdc: float
    unallocated_usdc: float
    n_markets: int
    daily_return_pct: float  # fraction: total_share_adj / bankroll
    max_concentration: float
    max_markets_used: int = 0
    max_markets_recommended: int = 0
    as_weight: float = DEFAULT_AS_WEIGHT
    note: str = (
        "return_%/day ≈ total_share_adjusted / capital. "
        "Efficiency uses AS haircut so thin toxic books don't win on raw pool share. "
        "As capital grows, % naturally declines when reward surface is finite."
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bankroll_usdc": round(self.bankroll_usdc, 4),
            "n_markets": self.n_markets,
            "total_share_adjusted_usdc": round(self.total_share_adjusted_usdc, 6),
            "total_risk_adjusted_usdc": round(self.total_risk_adjusted_usdc, 6),
            "total_allocated_usdc": round(self.total_allocated_usdc, 4),
            "unallocated_usdc": round(self.unallocated_usdc, 4),
            "daily_return_pct": round(self.daily_return_pct, 8),
            "daily_return_pct_display": round(self.daily_return_pct * 100.0, 4),
            "max_concentration": self.max_concentration,
            "max_markets_used": self.max_markets_used,
            "max_markets_recommended": self.max_markets_recommended,
            "as_weight": self.as_weight,
            "picks": [p.as_dict() for p in self.picks],
            "note": self.note,
            "headline_kpi": "total_risk_adjusted_usdc",
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


def _as_risk_for_market(m: dict[str, Any]) -> float:
    if m.get("as_risk") is not None:
        try:
            return min(1.0, max(0.0, float(m["as_risk"])))
        except (TypeError, ValueError):
            pass
    return as_risk_proxy(
        liquidity_num=float(m.get("liquidity_num") or m.get("market_liquidity") or 0.0),
        rewards_max_spread=float(m.get("rewards_max_spread") or 3.0),
        typical_price=float(m.get("typical_price") or m.get("mid") or 0.5),
    )


def optimize_multi_market_portfolio(
    markets: Sequence[dict[str, Any]],
    *,
    bankroll_usdc: float,
    max_markets: int = 20,
    max_concentration: float = 0.40,
    uptime: float = DEFAULT_UPTIME,
    max_share: float = DEFAULT_MAX_SHARE,
    as_weight: float = DEFAULT_AS_WEIGHT,
    auto_max_markets: bool = False,
    hard_cap_markets: int = 20,
) -> MultiMarketPortfolio:
    """Greedy multi-market pick: maximize Σ **risk-adjusted** share under bankroll.

    Algorithm (efficient, pure, no I/O):
      1. Universe unbounded; slots = ``max_markets`` (or auto-chosen if
         ``auto_max_markets``).
      2. Efficiency = risk_adjust(share_adj) / allocated_usdc — AS haircut so
         ultra-thin toxic books don't beat safer dominable books.
      3. Slice = min(remaining, bankroll × max_concentration); skip ineligible.

    When ``auto_max_markets`` is True, tries several slot counts and keeps the
    portfolio with highest total risk-adjusted expected USDC.
    """
    b = max(0.0, float(bankroll_usdc))
    aw = min(1.0, max(0.0, float(as_weight)))
    hard = max(1, int(hard_cap_markets))

    if auto_max_markets and b > 0 and markets:
        return _optimize_with_dynamic_slots(
            markets,
            bankroll_usdc=b,
            max_concentration=max_concentration,
            uptime=uptime,
            max_share=max_share,
            as_weight=aw,
            hard_cap_markets=hard,
        )

    return _optimize_fixed_slots(
        markets,
        bankroll_usdc=b,
        max_markets=max(0, int(max_markets)),
        max_concentration=max_concentration,
        uptime=uptime,
        max_share=max_share,
        as_weight=aw,
        max_markets_recommended=int(max_markets),
    )


def _optimize_fixed_slots(
    markets: Sequence[dict[str, Any]],
    *,
    bankroll_usdc: float,
    max_markets: int,
    max_concentration: float,
    uptime: float,
    max_share: float,
    as_weight: float,
    max_markets_recommended: int,
) -> MultiMarketPortfolio:
    b = bankroll_usdc
    if b <= 0 or not markets or max_markets <= 0:
        return MultiMarketPortfolio(
            bankroll_usdc=b,
            picks=(),
            total_share_adjusted_usdc=0.0,
            total_risk_adjusted_usdc=0.0,
            total_allocated_usdc=0.0,
            unallocated_usdc=b,
            n_markets=0,
            daily_return_pct=0.0,
            max_concentration=max_concentration,
            max_markets_used=0,
            max_markets_recommended=max_markets_recommended,
            as_weight=as_weight,
        )

    remaining = b
    picks: list[PortfolioPick] = []
    used: set[str] = set()
    max_slice = max(1e-6, b * float(max_concentration))
    # Index markets by cid for as_risk
    by_cid = {str(m.get("condition_id") or ""): m for m in markets}

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
            as_r = _as_risk_for_market(m)
            ra = risk_adjust_share_adj(
                plan.share_adjusted_expected_usdc, as_r, as_weight=as_weight
            )
            if ra <= 0:
                continue
            raw_eff = plan.share_adjusted_expected_usdc / alloc
            eff = ra / alloc
            if eff > best_eff + 1e-15 or (
                abs(eff - best_eff) <= 1e-15
                and best is not None
                and ra > best.risk_adjusted_share_usdc
            ):
                best_eff = eff
                best = PortfolioPick(
                    condition_id=cid,
                    allocated_usdc=alloc,
                    plan=plan,
                    efficiency=eff,
                    as_risk=as_r,
                    risk_adjusted_share_usdc=ra,
                    raw_efficiency=raw_eff,
                )
        if best is None:
            break
        picks.append(best)
        used.add(best.condition_id)
        remaining -= best.allocated_usdc
        _ = by_cid  # keep for clarity / future use

    total_sa = sum(p.plan.share_adjusted_expected_usdc for p in picks)
    total_ra = sum(p.risk_adjusted_share_usdc for p in picks)
    total_alloc = sum(p.allocated_usdc for p in picks)
    pct = (total_sa / b) if b > 0 else 0.0
    return MultiMarketPortfolio(
        bankroll_usdc=b,
        picks=tuple(picks),
        total_share_adjusted_usdc=total_sa,
        total_risk_adjusted_usdc=total_ra,
        total_allocated_usdc=total_alloc,
        unallocated_usdc=max(0.0, remaining),
        n_markets=len(picks),
        daily_return_pct=pct,
        max_concentration=float(max_concentration),
        max_markets_used=len(picks),
        max_markets_recommended=max_markets_recommended,
        as_weight=as_weight,
    )


def recommend_max_markets(
    markets: Sequence[dict[str, Any]],
    *,
    bankroll_usdc: float,
    hard_cap: int = 20,
    max_concentration: float = 0.40,
    as_weight: float = DEFAULT_AS_WEIGHT,
    candidate_slots: Sequence[int] = (1, 2, 3, 4, 6, 8, 10, 12, 16, 20),
) -> int:
    """Choose simultaneous market slots that maximize risk-adjusted portfolio $.

    Not a fixed N — best N for *this* capital and reward surface.
    """
    b = max(0.0, float(bankroll_usdc))
    hard = max(1, int(hard_cap))
    if b <= 0 or not markets:
        return 1
    best_n = 1
    best_ra = -1.0
    for n in candidate_slots:
        n_i = int(n)
        if n_i < 1 or n_i > hard:
            continue
        port = _optimize_fixed_slots(
            markets,
            bankroll_usdc=b,
            max_markets=n_i,
            max_concentration=max_concentration,
            uptime=DEFAULT_UPTIME,
            max_share=DEFAULT_MAX_SHARE,
            as_weight=as_weight,
            max_markets_recommended=n_i,
        )
        if port.total_risk_adjusted_usdc > best_ra + 1e-12:
            best_ra = port.total_risk_adjusted_usdc
            best_n = n_i
        elif (
            abs(port.total_risk_adjusted_usdc - best_ra) <= 1e-12
            and port.n_markets > 0
            and n_i < best_n
        ):
            # Prefer fewer markets on ties (less operational complexity)
            best_n = n_i
    return best_n


def _optimize_with_dynamic_slots(
    markets: Sequence[dict[str, Any]],
    *,
    bankroll_usdc: float,
    max_concentration: float,
    uptime: float,
    max_share: float,
    as_weight: float,
    hard_cap_markets: int,
) -> MultiMarketPortfolio:
    n = recommend_max_markets(
        markets,
        bankroll_usdc=bankroll_usdc,
        hard_cap=hard_cap_markets,
        max_concentration=max_concentration,
        as_weight=as_weight,
    )
    return _optimize_fixed_slots(
        markets,
        bankroll_usdc=bankroll_usdc,
        max_markets=n,
        max_concentration=max_concentration,
        uptime=uptime,
        max_share=max_share,
        as_weight=as_weight,
        max_markets_recommended=n,
    )


def build_dominator_operator_report(
    portfolio: MultiMarketPortfolio,
    capacity: CapacityCurve,
) -> dict[str, Any]:
    """Operator-facing KPI surface: absolute $, %, outgrew physics, actions."""
    pct = portfolio.daily_return_pct * 100.0
    peak_pct = capacity.peak_pct * 100.0
    outgrew = capacity.capital_outgrew_reward_surface

    if outgrew:
        message = (
            f"Capital outgrew the dense reward surface: current ~{pct:.2f}%/day "
            f"vs peak ~{peak_pct:.2f}%/day at ${capacity.peak_pct_bankroll:.0f}. "
            "This is physics (finite pools), not a broken strategy. "
            "Absolute share-adjusted $ may still be rising — optimize $ and risk, "
            "not last week's %."
        )
        actions = [
            "Scan more thin, eligible books to restore share_of_pool",
            "Accept lower %/day if absolute $/day still grows with capital",
            "Avoid chasing monopoly ceilings on fat contested markets",
            "Keep reward-min sizing; undersized quotes earn $0 share",
        ]
    elif portfolio.n_markets == 0:
        message = (
            "No eligible markets at this bankroll (rewardsMinSize / two-sided "
            "capital gate). Increase capital or pick markets with lower min size."
        )
        actions = [
            "Lower rewards_min_size targets or raise bankroll",
            "Run capital scenarios (tight vs sufficient) before live",
        ]
    else:
        message = (
            f"Portfolio live: {portfolio.n_markets} markets, "
            f"~${portfolio.total_share_adjusted_usdc:.2f}/day share-adj "
            f"(~${portfolio.total_risk_adjusted_usdc:.2f} AS-haircut), "
            f"~{pct:.2f}%/day on ${portfolio.bankroll_usdc:.0f}. "
            f"Slots recommended={portfolio.max_markets_recommended}."
        )
        actions = [
            "Defend in-band size-eligible quotes to hold share_of_pool",
            "Re-run capacity_curve after capital compounds",
            "Prefer picks with high efficiency and moderate as_risk",
        ]

    return {
        "headline_kpi": "total_risk_adjusted_usdc",
        "bankroll_usdc": portfolio.bankroll_usdc,
        "n_markets": portfolio.n_markets,
        "total_share_adjusted_usdc": round(portfolio.total_share_adjusted_usdc, 6),
        "total_risk_adjusted_usdc": round(portfolio.total_risk_adjusted_usdc, 6),
        "daily_return_pct_display": round(pct, 4),
        "peak_pct_display": round(peak_pct, 4),
        "peak_pct_bankroll": capacity.peak_pct_bankroll,
        "capital_outgrew_reward_surface": outgrew,
        "outgrew_reason": capacity.outgrew_reason,
        "operator_message": message,
        "recommended_actions": actions,
        "max_markets_recommended": portfolio.max_markets_recommended,
        "picks_summary": [
            {
                "condition_id": p.condition_id,
                "allocated_usdc": round(p.allocated_usdc, 2),
                "share_adj": round(p.plan.share_adjusted_expected_usdc, 4),
                "as_risk": round(p.as_risk, 3),
                "efficiency": round(p.efficiency, 6),
            }
            for p in portfolio.picks
        ],
        "note": capacity.note,
    }


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
    if peak.daily_return_pct > 1e-12 and cur_b > peak.bankroll_usdc * 1.05 and cur_pct < peak.daily_return_pct * float(outgrew_frac_of_peak):
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
