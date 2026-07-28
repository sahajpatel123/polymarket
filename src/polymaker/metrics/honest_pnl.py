"""Honest PnL decomposition — no monopoly-reward victory laps.

Produces the views a financial claim must show:
  - PnL without rewards
  - Rewards under conservative / base / optimistic competition shares
  - AS-adjusted (markout-haircut) spread
  - Size-eligible in-band accrual only (undersized quotes earn $0)

A synthetic run that only looks good under monopoly + instant mid-edge
cannot be labeled PASS without these non-monopoly views present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Competition share assumptions for maker reward pool
REWARD_SHARE_CONSERVATIVE = 0.01   # 1% of pool
REWARD_SHARE_BASE = 0.05           # 5% of pool
REWARD_SHARE_OPTIMISTIC = 0.20     # 20% of pool
REWARD_SHARE_MONOPOLY = 1.00       # diagnostic only — never financial PASS


@dataclass
class HonestPnL:
    """Decomposition of a metrics run into honest economic views."""

    # Core components
    instant_spread_usdc: float = 0.0
    as_adjusted_spread_usdc: float = 0.0  # instant + signed markout haircut
    markout_30s_mean: float = 0.0
    markout_n: int = 0
    total_fill_shares: float = 0.0
    n_fill: int = 0
    n_quote: int = 0

    # Raw monopoly accrual (diagnostic; not for promotion)
    monopoly_reward_usdc: float = 0.0
    # Eligible in-band seconds (size >= rewards_min_size)
    eligible_in_band_seconds: float = 0.0
    # In-band seconds that were undersized (should not earn)
    undersized_in_band_seconds: float = 0.0
    rewards_daily_rate: float = 0.0

    # Competition-share rewards
    reward_conservative_usdc: float = 0.0
    reward_base_usdc: float = 0.0
    reward_optimistic_usdc: float = 0.0
    # Measured or modeled share of the reward pool (realistic / monopoly)
    share_of_pool: float = 0.0
    share_adjusted_reward_usdc: float = 0.0  # HEADLINE reward view

    # Headline nets
    pnl_without_rewards_usdc: float = 0.0          # AS-adjusted spread only
    pnl_conservative_usdc: float = 0.0             # AS-spread + cons. rewards
    pnl_base_usdc: float = 0.0
    pnl_optimistic_usdc: float = 0.0
    pnl_monopoly_diagnostic_usdc: float = 0.0      # never use for PASS
    pnl_share_adjusted_usdc: float = 0.0           # AS-spread + share-adjusted rewards

    # Claim labels
    financial_claim_ok: bool = False
    claim_blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instant_spread_usdc": round(self.instant_spread_usdc, 6),
            "as_adjusted_spread_usdc": round(self.as_adjusted_spread_usdc, 6),
            "markout_30s_mean": round(self.markout_30s_mean, 6),
            "markout_n": self.markout_n,
            "total_fill_shares": round(self.total_fill_shares, 4),
            "n_fill": self.n_fill,
            "n_quote": self.n_quote,
            "monopoly_reward_usdc": round(self.monopoly_reward_usdc, 6),
            "eligible_in_band_seconds": round(self.eligible_in_band_seconds, 3),
            "undersized_in_band_seconds": round(self.undersized_in_band_seconds, 3),
            "rewards_daily_rate": self.rewards_daily_rate,
            "reward_conservative_usdc": round(self.reward_conservative_usdc, 6),
            "reward_base_usdc": round(self.reward_base_usdc, 6),
            "reward_optimistic_usdc": round(self.reward_optimistic_usdc, 6),
            "share_of_pool": round(self.share_of_pool, 6),
            "share_adjusted_reward_usdc": round(self.share_adjusted_reward_usdc, 6),
            "pnl_without_rewards_usdc": round(self.pnl_without_rewards_usdc, 6),
            "pnl_conservative_usdc": round(self.pnl_conservative_usdc, 6),
            "pnl_base_usdc": round(self.pnl_base_usdc, 6),
            "pnl_optimistic_usdc": round(self.pnl_optimistic_usdc, 6),
            "pnl_share_adjusted_usdc": round(self.pnl_share_adjusted_usdc, 6),
            "pnl_monopoly_diagnostic_usdc": round(self.pnl_monopoly_diagnostic_usdc, 6),
            "financial_claim_ok": self.financial_claim_ok,
            "claim_blockers": list(self.claim_blockers),
            "headline_kpi": "share_adjusted_reward_usdc",
        }


def compute_honest_pnl(
    *,
    instant_spread_usdc: float,
    markout_30s_mean: float = 0.0,
    markout_n: int = 0,
    total_fill_shares: float = 0.0,
    n_fill: int = 0,
    n_quote: int = 0,
    rewards_daily_rate: float = 0.0,
    eligible_in_band_seconds: float = 0.0,
    undersized_in_band_seconds: float = 0.0,
    monopoly_reward_usdc: float | None = None,
    share_adjusted_reward_usdc: float | None = None,
    share_of_pool: float | None = None,
    min_fills_for_claim: int = 10,
    min_quotes_for_claim: int = 50,
) -> HonestPnL:
    """Build honest PnL views from analyzer components.

    AS haircut: mean 30s markout (signed, + good for us) × fill shares.
    When markouts are negative, as_adjusted_spread < instant_spread.

    Headline reward view is **share_adjusted_reward_usdc** (or base share of
    eligible pool when not measured). Monopoly is diagnostic only.
    """
    h = HonestPnL(
        instant_spread_usdc=float(instant_spread_usdc),
        markout_30s_mean=float(markout_30s_mean),
        markout_n=int(markout_n),
        total_fill_shares=float(total_fill_shares),
        n_fill=int(n_fill),
        n_quote=int(n_quote),
        rewards_daily_rate=float(rewards_daily_rate),
        eligible_in_band_seconds=float(eligible_in_band_seconds),
        undersized_in_band_seconds=float(undersized_in_band_seconds),
    )

    # Markout haircut on filled shares (negative markout hurts)
    as_pnl = h.markout_30s_mean * h.total_fill_shares if h.markout_n > 0 else 0.0
    h.as_adjusted_spread_usdc = h.instant_spread_usdc + as_pnl

    # Eligible reward base: only size-eligible in-band time
    eligible_frac = h.eligible_in_band_seconds / 86400.0
    eligible_pool = h.rewards_daily_rate * eligible_frac

    if monopoly_reward_usdc is not None:
        # Diagnostic: raw analyzer monopoly (may include undersized time)
        h.monopoly_reward_usdc = float(monopoly_reward_usdc)
    else:
        h.monopoly_reward_usdc = eligible_pool  # still 100% of eligible — not share

    h.reward_conservative_usdc = eligible_pool * REWARD_SHARE_CONSERVATIVE
    h.reward_base_usdc = eligible_pool * REWARD_SHARE_BASE
    h.reward_optimistic_usdc = eligible_pool * REWARD_SHARE_OPTIMISTIC

    # Share-adjusted headline: measured first, else base competition share
    if share_adjusted_reward_usdc is not None:
        h.share_adjusted_reward_usdc = max(0.0, float(share_adjusted_reward_usdc))
    else:
        h.share_adjusted_reward_usdc = h.reward_base_usdc

    if share_of_pool is not None:
        h.share_of_pool = max(0.0, min(1.0, float(share_of_pool)))
    elif h.monopoly_reward_usdc > 1e-12:
        h.share_of_pool = min(1.0, h.share_adjusted_reward_usdc / h.monopoly_reward_usdc)
    elif eligible_pool > 1e-12:
        h.share_of_pool = min(1.0, h.share_adjusted_reward_usdc / eligible_pool)
    else:
        h.share_of_pool = 0.0

    h.pnl_without_rewards_usdc = h.as_adjusted_spread_usdc
    h.pnl_conservative_usdc = h.as_adjusted_spread_usdc + h.reward_conservative_usdc
    h.pnl_base_usdc = h.as_adjusted_spread_usdc + h.reward_base_usdc
    h.pnl_optimistic_usdc = h.as_adjusted_spread_usdc + h.reward_optimistic_usdc
    h.pnl_share_adjusted_usdc = h.as_adjusted_spread_usdc + h.share_adjusted_reward_usdc
    h.pnl_monopoly_diagnostic_usdc = (
        h.instant_spread_usdc + h.monopoly_reward_usdc
    )

    blockers: list[str] = []
    if h.n_fill < min_fills_for_claim:
        blockers.append(f"n_fill={h.n_fill}<{min_fills_for_claim}")
    if h.n_quote < min_quotes_for_claim:
        blockers.append(f"n_quote={h.n_quote}<{min_quotes_for_claim}")
    # Monopoly-only jackpot: without-rewards bad but monopoly looks great
    if (
        h.pnl_without_rewards_usdc <= 0
        and h.pnl_monopoly_diagnostic_usdc > 0
        and h.monopoly_reward_usdc > abs(h.pnl_without_rewards_usdc)
    ):
        blockers.append("monopoly_rewards_only_positive")
    if h.undersized_in_band_seconds > h.eligible_in_band_seconds * 2 and h.monopoly_reward_usdc > 0:
        blockers.append("mostly_undersized_in_band_quotes")
    if h.markout_n > 0 and h.as_adjusted_spread_usdc < h.instant_spread_usdc * 0.5:
        # heavy AS — note but not auto-block if still positive after haircut
        if h.as_adjusted_spread_usdc < 0:
            blockers.append("as_adjusted_spread_negative")
    # Explicit: monopoly cannot be the sole PASS when share-adjusted is weak
    if (
        h.pnl_monopoly_diagnostic_usdc > 0
        and h.pnl_share_adjusted_usdc <= 0
        and h.share_of_pool < 0.02
    ):
        blockers.append("monopoly_only_share_near_zero")

    h.claim_blockers = blockers
    # Financial claim OK only if share-adjusted/conservative path positive and
    # no blockers. Monopoly alone is never sufficient.
    h.financial_claim_ok = (
        len(blockers) == 0
        and h.pnl_share_adjusted_usdc > 0
        and h.pnl_conservative_usdc > 0
        and h.n_fill >= min_fills_for_claim
        and h.n_quote >= min_quotes_for_claim
    )
    return h


@dataclass(frozen=True)
class AspirationalVsHonest:
    """Aspirational daily return target vs honest realized components.

    Monopoly diagnostic is recorded for audit but never used as the sole
    financial PASS flag.
    """

    aspirational_low_pct: float
    aspirational_high_pct: float
    bankroll_usdc: float
    target_pnl_low_usdc: float
    target_pnl_high_usdc: float
    realized_without_rewards_usdc: float
    realized_conservative_usdc: float
    realized_base_usdc: float
    realized_optimistic_usdc: float
    monopoly_diagnostic_usdc: float
    realized_return_conservative_pct: float
    gap_to_low_usdc: float
    within_aspirational_band: bool
    financial_pass_ok: bool
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "aspirational_low_pct": self.aspirational_low_pct,
            "aspirational_high_pct": self.aspirational_high_pct,
            "bankroll_usdc": round(self.bankroll_usdc, 4),
            "target_pnl_low_usdc": round(self.target_pnl_low_usdc, 6),
            "target_pnl_high_usdc": round(self.target_pnl_high_usdc, 6),
            "realized_without_rewards_usdc": round(self.realized_without_rewards_usdc, 6),
            "realized_conservative_usdc": round(self.realized_conservative_usdc, 6),
            "realized_base_usdc": round(self.realized_base_usdc, 6),
            "realized_optimistic_usdc": round(self.realized_optimistic_usdc, 6),
            "monopoly_diagnostic_usdc": round(self.monopoly_diagnostic_usdc, 6),
            "realized_return_conservative_pct": round(
                self.realized_return_conservative_pct, 6
            ),
            "gap_to_low_usdc": round(self.gap_to_low_usdc, 6),
            "within_aspirational_band": self.within_aspirational_band,
            "financial_pass_ok": self.financial_pass_ok,
            "note": self.note,
        }


def compare_aspirational_vs_honest(
    *,
    bankroll_usdc: float,
    honest: HonestPnL,
    aspirational_low: float = 0.10,
    aspirational_high: float = 0.15,
) -> AspirationalVsHonest:
    """Compare user aspirational 10–15%/day band to honest realized PnL.

    Uses **conservative** competition-share rewards + AS-adjusted spread as
    the primary realized figure. Monopoly is never sufficient for
    ``financial_pass_ok`` or ``within_aspirational_band``.
    """
    b = max(0.0, float(bankroll_usdc))
    lo = max(0.0, float(aspirational_low))
    hi = max(lo, float(aspirational_high))
    target_lo = b * lo
    target_hi = b * hi
    cons = float(honest.pnl_conservative_usdc)
    ret_pct = (cons / b) if b > 0 else 0.0
    within = bool(b > 0 and target_lo <= cons <= target_hi * 1.05)
    # Explicit: monopoly alone cannot mark success
    monopoly_only = (
        honest.pnl_without_rewards_usdc <= 0
        and honest.pnl_monopoly_diagnostic_usdc > target_lo
        and cons < target_lo
    )
    note = (
        "aspirational_target_only_not_a_guarantee; "
        "financial_pass uses conservative+AS views only"
    )
    if monopoly_only:
        note += "; monopoly_diagnostic_excluded_from_pass"
    return AspirationalVsHonest(
        aspirational_low_pct=lo * 100.0,
        aspirational_high_pct=hi * 100.0,
        bankroll_usdc=b,
        target_pnl_low_usdc=target_lo,
        target_pnl_high_usdc=target_hi,
        realized_without_rewards_usdc=float(honest.pnl_without_rewards_usdc),
        realized_conservative_usdc=cons,
        realized_base_usdc=float(honest.pnl_base_usdc),
        realized_optimistic_usdc=float(honest.pnl_optimistic_usdc),
        monopoly_diagnostic_usdc=float(honest.pnl_monopoly_diagnostic_usdc),
        realized_return_conservative_pct=ret_pct * 100.0,
        gap_to_low_usdc=target_lo - cons,
        within_aspirational_band=within and honest.financial_claim_ok and not monopoly_only,
        financial_pass_ok=bool(honest.financial_claim_ok) and not monopoly_only,
        note=note,
    )


def honest_pnl_from_report(rep: Any, *, events: list[dict[str, Any]] | None = None) -> HonestPnL:
    """Derive HonestPnL from a MetricsReport (+ optional raw events for sizes)."""
    # Total fill shares from events if provided
    total_shares = 0.0
    eligible_s = 0.0
    undersized_s = 0.0
    daily = 0.0
    rewards_min = 0.0

    if events:
        # Reconstruct eligible vs undersized in-band time from quote sizes
        live: dict[str, dict[str, tuple[bool, float, float]]] = {}
        # oid -> (in_band, size, ts_placed)
        samples: list[tuple[float, bool, bool]] = []  # ts, any_eligible_band, any_undersized_band
        meta_min: dict[str, float] = {}
        meta_daily: dict[str, float] = {}

        def _push(ts: float) -> None:
            any_el = False
            any_un = False
            for oid_map in live.values():
                for in_b, sz, _ in oid_map.values():
                    if not in_b:
                        continue
                    # need cid-level min — use max min across
                    rmin = max(meta_min.values()) if meta_min else 0.0
                    if rmin <= 0 or sz + 1e-12 >= rmin:
                        any_el = True
                    else:
                        any_un = True
            samples.append((ts, any_el, any_un))

        for e in events:
            ev = e.get("event")
            cid = str(e.get("condition_id") or "")
            ts = float(e.get("ts") or 0.0)
            if ev == "market_meta":
                meta_daily[cid] = float(e.get("rewards_daily_rate") or 0.0)
                meta_min[cid] = float(e.get("rewards_min_size") or 0.0)
                daily = max(daily, meta_daily[cid])
                rewards_min = max(rewards_min, meta_min[cid])
            elif ev == "quote":
                oid = str(e.get("order_id") or "")
                in_b = bool(e.get("in_reward_band", False))
                sz = float(e.get("size") or 0.0)
                live.setdefault(cid, {})[oid] = (in_b, sz, ts)
                _push(ts)
            elif ev == "cancel":
                oid = str(e.get("order_id") or "")
                if cid in live:
                    live[cid].pop(oid, None)
                _push(ts)
            elif ev == "fill":
                total_shares += float(e.get("size") or 0.0)
            elif ev == "mark":
                _push(ts)

        # Integrate samples
        samples.sort(key=lambda x: x[0])
        last_t = None
        last_el = False
        last_un = False
        for t, el, un in samples:
            if last_t is not None:
                dt = max(0.0, t - last_t)
                if last_el:
                    eligible_s += dt
                if last_un and not last_el:
                    undersized_s += dt
            last_t, last_el, last_un = t, el, un
    else:
        # Fallback: use report monopoly as upper diagnostic; no eligibility split
        monopoly = sum(getattr(rep, "reward_accrual_usdc", {}).values())
        eligible_s = sum(getattr(rep, "in_band_seconds", {}).values())
        total_shares = 0.0  # unknown
        daily = 0.0
        for v in getattr(rep, "rebate_pool_daily_usdc", {}).values():
            pass
        # try to get daily from reward if in_band known
        in_s = sum(getattr(rep, "in_band_seconds", {}).values())
        mon = sum(getattr(rep, "reward_accrual_usdc", {}).values())
        if in_s > 0 and mon > 0:
            daily = mon / (in_s / 86400.0)

    monopoly = sum(getattr(rep, "reward_accrual_usdc", {}).values())
    m30 = float(getattr(rep, "markout", {}).get("30s", 0.0) or 0.0)
    m_n = int(getattr(rep, "markout_n", {}).get("30s", 0) or 0)

    # If total_shares unknown, approximate from n_fill (weak)
    if total_shares <= 0 and getattr(rep, "n_fill", 0) > 0:
        total_shares = float(rep.n_fill)  # 1 share/fill lower bound

    # Measured share-adjusted if report carries reward_our / total_est
    measured_share_adj = getattr(rep, "reward_our_usdc", None)
    if measured_share_adj is None and isinstance(getattr(rep, "honest_pnl", None), dict):
        measured_share_adj = rep.honest_pnl.get("share_adjusted_reward_usdc")
    measured_share = None
    if monopoly and monopoly > 0 and measured_share_adj is not None:
        measured_share = float(measured_share_adj) / float(monopoly)

    return compute_honest_pnl(
        instant_spread_usdc=float(getattr(rep, "realized_spread_usdc", 0.0) or 0.0),
        markout_30s_mean=m30,
        markout_n=m_n,
        total_fill_shares=total_shares,
        n_fill=int(getattr(rep, "n_fill", 0) or 0),
        n_quote=int(getattr(rep, "n_quote", 0) or 0),
        rewards_daily_rate=daily,
        eligible_in_band_seconds=eligible_s,
        undersized_in_band_seconds=undersized_s,
        monopoly_reward_usdc=monopoly,
        share_adjusted_reward_usdc=(
            float(measured_share_adj) if measured_share_adj is not None else None
        ),
        share_of_pool=measured_share,
    )
