"""Capital feasibility: never silently run with zero valid orders.

Also owns the **maker reward-eligibility gate** used on the live quote path:
floor size to rewardsMinSize when capital can fund a two-sided cycle, or
skip/refuse the market with an explicit reason when it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapitalCheck:
    ok: bool
    reason: str
    min_order_notional: float
    required_for_two_sided: float
    bankroll: float
    affordable_price_max: float  # max quote price still affordable for min shares

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "min_order_notional": self.min_order_notional,
            "required_for_two_sided": self.required_for_two_sided,
            "bankroll": self.bankroll,
            "affordable_price_max": self.affordable_price_max,
        }


def check_capital_feasibility(
    *,
    bankroll: float,
    exchange_min_shares: float,
    reward_min_shares: float,
    typical_price: float = 0.5,
    layers: int = 1,
    two_sided: bool = True,
    safety_frac: float = 0.5,
) -> CapitalCheck:
    """Decide whether bankroll can place at least one valid order cycle.

    minimum_order_notional = max(exchange_min, reward_min) × price
    two-sided cycle needs ~2 × layers × min notional (YES + NO entries),
    and we require bankroll * safety_frac to cover that inventory risk.
    """
    min_shares = max(float(exchange_min_shares), float(reward_min_shares), 0.0)
    px = min(max(float(typical_price), 0.01), 0.99)
    min_notional = min_shares * px
    sides = 2 if two_sided else 1
    required = min_notional * max(layers, 1) * sides
    usable = bankroll * safety_frac
    affordable_max = (usable / sides / max(layers, 1) / min_shares) if min_shares > 0 else 0.0

    if bankroll <= 0:
        return CapitalCheck(
            ok=False,
            reason="bankroll_non_positive",
            min_order_notional=min_notional,
            required_for_two_sided=required,
            bankroll=bankroll,
            affordable_price_max=0.0,
        )
    if min_shares <= 0:
        return CapitalCheck(
            ok=True,
            reason="no_min_size",
            min_order_notional=0.0,
            required_for_two_sided=0.0,
            bankroll=bankroll,
            affordable_price_max=1.0,
        )
    if usable < min_notional:
        return CapitalCheck(
            ok=False,
            reason=(
                f"INSUFFICIENT_CAPITAL: usable={usable:.2f} < "
                f"min_order_notional={min_notional:.2f} "
                f"(min_shares={min_shares} @ px={px})"
            ),
            min_order_notional=min_notional,
            required_for_two_sided=required,
            bankroll=bankroll,
            affordable_price_max=affordable_max,
        )
    if usable < required:
        return CapitalCheck(
            ok=False,
            reason=(
                f"INSUFFICIENT_CAPITAL: usable={usable:.2f} < "
                f"two_sided_cycle={required:.2f}"
            ),
            min_order_notional=min_notional,
            required_for_two_sided=required,
            bankroll=bankroll,
            affordable_price_max=affordable_max,
        )
    return CapitalCheck(
        ok=True,
        reason="capital_ok",
        min_order_notional=min_notional,
        required_for_two_sided=required,
        bankroll=bankroll,
        affordable_price_max=min(0.99, affordable_max),
    )


# ── Live maker path: floor size or skip market ─────────────────────────


@dataclass(frozen=True)
class MakerRewardEligibility:
    """Result of :func:`decide_maker_reward_eligibility`.

    * ``skip=True`` — do not place maker quotes; ``reason`` is operator-visible.
    * ``skip=False`` and ``eligible=True`` — size at least ``recommended_base_size_usdc``
      (USDC notional for one reward-min order at typical price).
    * ``skip=False`` and ``eligible=False`` with ``reason=bankroll_unset`` —
      no capital configured; leave sizing to the profile (legacy path).
    """

    eligible: bool
    skip: bool
    reason: str
    min_shares: float
    min_order_notional_usdc: float
    required_two_sided_usdc: float
    recommended_base_size_usdc: float
    bankroll_usdc: float
    typical_price: float

    def as_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "skip": self.skip,
            "reason": self.reason,
            "min_shares": self.min_shares,
            "min_order_notional_usdc": self.min_order_notional_usdc,
            "required_two_sided_usdc": self.required_two_sided_usdc,
            "recommended_base_size_usdc": self.recommended_base_size_usdc,
            "bankroll_usdc": self.bankroll_usdc,
            "typical_price": self.typical_price,
        }


def decide_maker_reward_eligibility(
    *,
    bankroll_usdc: float,
    rewards_min_size: float,
    exchange_min_shares: float = 5.0,
    typical_price: float = 0.5,
    layers: int = 1,
    safety_frac: float = 0.5,
    reward_size_mult: float = 1.0,
    default_base_size_usdc: float = 0.0,
) -> MakerRewardEligibility:
    """Floor-or-skip gate for reward-eligible maker quoting.

    When ``bankroll_usdc <= 0`` (unset), returns non-skip so legacy configs
    without a bankroll still quote. When rewards min is zero, no reward
    floor is required.

    When capital is set and rewards min > 0:
    - If a two-sided cycle at min shares is affordable → ``eligible``,
      ``recommended_base_size_usdc`` ≥ reward notional × mult.
    - Else → ``skip=True`` with an explicit ``INSUFFICIENT_CAPITAL`` reason
      (undersized silent farming is forbidden).
    """
    b = float(bankroll_usdc)
    rmin = max(0.0, float(rewards_min_size))
    xmin = max(0.0, float(exchange_min_shares))
    px = min(max(float(typical_price), 0.01), 0.99)
    mult = max(1.0, float(reward_size_mult))
    min_shares = max(xmin, rmin) * (mult if rmin > 0 else 1.0)
    # Keep share count as reward floor when mult expands notional via size
    if rmin > 0:
        min_shares = max(xmin, rmin * mult)
    else:
        min_shares = xmin

    # No bankroll configured → do not block (paper/legacy profiles).
    if b <= 0:
        return MakerRewardEligibility(
            eligible=False,
            skip=False,
            reason="bankroll_unset",
            min_shares=min_shares,
            min_order_notional_usdc=min_shares * px,
            required_two_sided_usdc=0.0,
            recommended_base_size_usdc=max(0.0, float(default_base_size_usdc)),
            bankroll_usdc=b,
            typical_price=px,
        )

    if rmin <= 0 and xmin <= 0:
        base = max(2.0, min(250.0, b * 0.10), float(default_base_size_usdc or 0.0))
        return MakerRewardEligibility(
            eligible=True,
            skip=False,
            reason="no_min_size",
            min_shares=0.0,
            min_order_notional_usdc=0.0,
            required_two_sided_usdc=0.0,
            recommended_base_size_usdc=base,
            bankroll_usdc=b,
            typical_price=px,
        )

    # Feasibility uses raw reward min (not mult) so mult is a size preference
    # only when capital already clears the two-sided bar.
    check = check_capital_feasibility(
        bankroll=b,
        exchange_min_shares=xmin,
        reward_min_shares=rmin,
        typical_price=px,
        layers=max(1, int(layers)),
        two_sided=True,
        safety_frac=safety_frac,
    )
    min_notional = max(xmin, rmin * mult) * px
    if not check.ok:
        return MakerRewardEligibility(
            eligible=False,
            skip=True,
            reason=check.reason,
            min_shares=max(xmin, rmin),
            min_order_notional_usdc=check.min_order_notional,
            required_two_sided_usdc=check.required_for_two_sided,
            recommended_base_size_usdc=0.0,
            bankroll_usdc=b,
            typical_price=px,
        )

    # Qualify: floor base size at reward-eligible notional; still scale with bankroll.
    bankroll_base = max(2.0, min(250.0, b * 0.10))
    floored = max(bankroll_base, min_notional, float(default_base_size_usdc or 0.0))
    # Never recommend more than 40% of bankroll on a single order notional.
    floored = min(floored, b * 0.40)
    # After the cap, if we dropped below reward min notional, we can no longer
    # claim eligibility — skip rather than undersize.
    reward_floor_usdc = (rmin * mult * px) if rmin > 0 else 0.0
    if reward_floor_usdc > 0 and floored + 1e-9 < reward_floor_usdc:
        return MakerRewardEligibility(
            eligible=False,
            skip=True,
            reason=(
                f"INSUFFICIENT_CAPITAL: reward_floor_usdc={reward_floor_usdc:.2f} "
                f"> max_single_order={b * 0.40:.2f} (40% of bankroll={b:.2f})"
            ),
            min_shares=max(xmin, rmin * mult),
            min_order_notional_usdc=reward_floor_usdc,
            required_two_sided_usdc=check.required_for_two_sided,
            recommended_base_size_usdc=0.0,
            bankroll_usdc=b,
            typical_price=px,
        )

    return MakerRewardEligibility(
        eligible=True,
        skip=False,
        reason="reward_eligible",
        min_shares=max(xmin, rmin * mult) if rmin > 0 else xmin,
        min_order_notional_usdc=max(check.min_order_notional, reward_floor_usdc),
        required_two_sided_usdc=check.required_for_two_sided,
        recommended_base_size_usdc=floored,
        bankroll_usdc=b,
        typical_price=px,
    )
