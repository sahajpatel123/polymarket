"""Capital feasibility: never silently run with zero valid orders."""

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
