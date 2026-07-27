"""Percent-based risk policy.

Replaces absolute USDC caps (e.g. ``daily_loss_kill_usdc = 250``) with
fractions of total capital. The single source of truth for the bot's
risk envelope is ``POLYMAKER_CAPITAL_USDC`` in ``.env``; every other
USDC cap is derived from it.

**Stops are percentages of capital. There is NO profit cap and NO
target growth field.** The bot earns as much as it can, subject only
to the loss-side caps below. If you compound a 10% day on top of a
10% day, the bot keeps compounding — the cap is a fraction of
*current* capital, not a fixed dollar ceiling.

Why percentages:
- One knob scales with bankroll, so a 10x capital change needs zero
  config edits.
- Loss caps stay proportional as the account grows.
- Behavior is reviewable: a reader can sanity-check the risk envelope
  without doing arithmetic.

Design rules:
- All policy fields are *fractions*, not USDC (e.g. ``0.10`` = 10%).
- Loss caps (stop loss, daily kill, drawdown kill, per-market kill)
  are *fractions of current capital*. As capital grows, the absolute
  USDC size of each stop grows with it; as it shrinks, the stops
  shrink too. The bot never stops "early" because of a fixed dollar
  cap left over from a smaller account.
- A :class:`RiskPolicy` can be resolved to absolute USDC values at any
  time given a capital number, but the resolved values are
  throwaway — never persisted, never read by humans. The source of
  truth is always the percentage.
- Hot-reload safe: the policy is a plain dataclass and re-resolves
  cleanly on every engine tick.

The single user knob:
    POLYMAKER_CAPITAL_USDC=500     # that's it; everything else is %
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ── Default policy constants ─────────────────────────────────────────
# All values are fractions of *current* capital. The bot has no
# target growth, no profit cap, no "you've made enough" threshold.

DEFAULT_MAX_PER_MARKET_PCT = 0.05          # 5% of capital per market, hard cap
DEFAULT_TOTAL_EXPOSURE_PCT = 1.0           # 100% deployable across all markets
DEFAULT_DAILY_LOSS_KILL_PCT = 0.10         # halt at -10% of capital in a day
DEFAULT_MAX_DRAWDOWN_KILL_PCT = 0.25       # halt at -25% from equity peak
DEFAULT_PER_MARKET_LOSS_PCT = 0.05         # reduce-only a market at -5% of capital
DEFAULT_PER_TRADE_LOSS_PCT = 0.005         # tight stop on a single fill (0.5%)
DEFAULT_MAX_CONCURRENT_MARKETS = 8
DEFAULT_MIN_REWARD_PCT_PER_DAY = 0.005     # skip markets < 0.5%/day expected

VALID_RISK_PROFILES = ("conservative", "balanced", "aggressive")


@dataclass(frozen=True)
class RiskProfile:
    """Named risk posture multipliers. Applied to LOSS-side defaults.

    The user picks a profile name once (``POLYMAKER_RISK_PROFILE``);
    we apply sensible multipliers on top of the base defaults.
    Multipliers affect how *tight* the loss caps are. There is no
    "target" multiplier because there is no target — the bot earns
    without ceiling.

    ``conservative``: 0.5x size caps, 0.5x daily/drawdown loss kill.
    ``balanced``:     1.0x everything (the factory defaults).
    ``aggressive``:   2.0x size caps, 2.0x daily/drawdown loss kill.
    """

    name: str
    size_mult: float
    loss_kill_mult: float
    max_markets: int

    @classmethod
    def from_name(cls, name: str) -> RiskProfile:
        if name is None or name == "":
            raise ValueError(
                f"unknown risk profile {name!r}; expected one of {VALID_RISK_PROFILES}"
            )
        n = name.strip().lower()
        if n not in VALID_RISK_PROFILES:
            raise ValueError(
                f"unknown risk profile {name!r}; expected one of {VALID_RISK_PROFILES}"
            )
        if n == "conservative":
            return cls(name=n, size_mult=0.5, loss_kill_mult=0.5, max_markets=4)
        if n == "aggressive":
            return cls(name=n, size_mult=2.0, loss_kill_mult=2.0, max_markets=12)
        return cls(name=n, size_mult=1.0, loss_kill_mult=1.0, max_markets=8)


@dataclass(frozen=True)
class RiskPolicy:
    """The single source of truth for percent-based risk.

    All fields are *fractions of capital* (0.10 = 10%). Resolution to
    USDC happens at the *call site* via :meth:`resolve` — never persist
    the resolved values.

    There is no ``target_*`` field. The bot has no profit cap. It
    earns without ceiling and stops only on the loss side.
    """

    # Per-market and total exposure caps
    max_per_market_pct: float = DEFAULT_MAX_PER_MARKET_PCT
    total_exposure_pct: float = DEFAULT_TOTAL_EXPOSURE_PCT

    # Daily / drawdown / per-market / per-trade loss caps
    daily_loss_kill_pct: float = DEFAULT_DAILY_LOSS_KILL_PCT
    max_drawdown_kill_pct: float = DEFAULT_MAX_DRAWDOWN_KILL_PCT
    per_market_loss_pct: float = DEFAULT_PER_MARKET_LOSS_PCT
    per_trade_loss_pct: float = DEFAULT_PER_TRADE_LOSS_PCT

    # Universe limits
    max_concurrent_markets: int = DEFAULT_MAX_CONCURRENT_MARKETS
    min_reward_pct_per_day: float = DEFAULT_MIN_REWARD_PCT_PER_DAY

    # Profile (informational; the multipliers are already applied above)
    profile_name: str = "balanced"

    # ── Construction ────────────────────────────────────────────────

    @classmethod
    def from_env(cls, profile_name: str | None = None) -> RiskPolicy:
        """Build a policy from the active risk profile.

        Profile multipliers are applied to the base defaults. Individual
        fields can still be overridden via env vars of the form
        ``POLYMAKER_<FIELD>_PCT`` (e.g. ``POLYMAKER_DAILY_LOSS_KILL_PCT=0.08``).
        """
        prof = RiskProfile.from_name(profile_name or os.environ.get("POLYMAKER_RISK_PROFILE", "balanced"))
        return cls(
            max_per_market_pct=cls._env_pct(
                "POLYMAKER_MAX_PER_MARKET_PCT", DEFAULT_MAX_PER_MARKET_PCT * prof.size_mult
            ),
            total_exposure_pct=cls._env_pct(
                "POLYMAKER_TOTAL_EXPOSURE_PCT", DEFAULT_TOTAL_EXPOSURE_PCT
            ),
            daily_loss_kill_pct=cls._env_pct(
                "POLYMAKER_DAILY_LOSS_KILL_PCT", DEFAULT_DAILY_LOSS_KILL_PCT * prof.loss_kill_mult
            ),
            max_drawdown_kill_pct=cls._env_pct(
                "POLYMAKER_MAX_DRAWDOWN_KILL_PCT", DEFAULT_MAX_DRAWDOWN_KILL_PCT * prof.loss_kill_mult
            ),
            per_market_loss_pct=cls._env_pct(
                "POLYMAKER_PER_MARKET_LOSS_PCT", DEFAULT_PER_MARKET_LOSS_PCT * prof.size_mult
            ),
            per_trade_loss_pct=cls._env_pct(
                "POLYMAKER_PER_TRADE_LOSS_PCT", DEFAULT_PER_TRADE_LOSS_PCT * prof.size_mult
            ),
            max_concurrent_markets=int(
                os.environ.get("POLYMAKER_MAX_MARKETS", str(prof.max_markets))
            ),
            min_reward_pct_per_day=cls._env_pct(
                "POLYMAKER_MIN_REWARD_PCT_PER_DAY", DEFAULT_MIN_REWARD_PCT_PER_DAY
            ),
            profile_name=prof.name,
        )

    @staticmethod
    def _env_pct(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            v = float(raw)
        except ValueError:
            return default
        # Heuristic: if user passes a whole number like "10", treat as 10%.
        if v > 1.0:
            v = v / 100.0
        return max(0.0, min(v, 1.0))

    # ── Resolution ──────────────────────────────────────────────────

    def resolve(self, capital_usdc: float) -> ResolvedPolicy:
        """Project percentages to USDC against a given capital.

        Accepts zero (paper / no capital) and returns a zero-everything
        resolved policy; rejects negative values. The resolved object
        contains ONLY loss-side caps — never a profit target.
        """
        if capital_usdc < 0:
            raise ValueError(
                "POLYMAKER_CAPITAL_USDC must be >= 0; got negative"
            )
        if capital_usdc == 0:
            return ResolvedPolicy(
                capital_usdc=0.0,
                max_per_market_usdc=0.0,
                total_exposure_usdc=0.0,
                daily_loss_kill_usdc=0.0,
                max_drawdown_kill_usdc=0.0,
                per_market_loss_usdc=0.0,
                per_trade_loss_usdc=0.0,
                min_reward_per_day_usdc=0.0,
                policy=self,
            )
        return ResolvedPolicy(
            capital_usdc=capital_usdc,
            max_per_market_usdc=capital_usdc * self.max_per_market_pct,
            total_exposure_usdc=capital_usdc * self.total_exposure_pct,
            daily_loss_kill_usdc=capital_usdc * self.daily_loss_kill_pct,
            max_drawdown_kill_usdc=capital_usdc * self.max_drawdown_kill_pct,
            per_market_loss_usdc=capital_usdc * self.per_market_loss_pct,
            per_trade_loss_usdc=capital_usdc * self.per_trade_loss_pct,
            min_reward_per_day_usdc=capital_usdc * self.min_reward_pct_per_day,
            policy=self,
        )


@dataclass(frozen=True)
class ResolvedPolicy:
    """USDC-projected view of a :class:`RiskPolicy`.

    Throwaway. The source of truth is the parent ``RiskPolicy``; this
    exists so the engine can compare against a stable float without
    re-deriving math inline.

    Fields are LOSS caps only. There is intentionally no profit target
    on this object — the bot earns without ceiling.
    """

    capital_usdc: float
    max_per_market_usdc: float
    total_exposure_usdc: float
    daily_loss_kill_usdc: float
    max_drawdown_kill_usdc: float
    per_market_loss_usdc: float
    per_trade_loss_usdc: float
    min_reward_per_day_usdc: float
    policy: RiskPolicy

    def scale_to(self, new_capital_usdc: float) -> ResolvedPolicy:
        """Re-resolve against a new capital value (used on hot-reload)."""
        return self.policy.resolve(new_capital_usdc)


def load_capital_usdc() -> float:
    """Read the single capital knob from the environment.

    Priority:
      1. ``POLYMAKER_CAPITAL_USDC`` (the V3 single knob)
      2. ``POLYMAKER_BANKROLL_USDC`` (legacy alias, kept for compat)
      3. 0.0 — caller decides what to do (paper mode, no real money)

    The returned value is *unvalidated* (could be 0, negative, or
    garbage). :meth:`RiskPolicy.resolve` is the validator.
    """
    raw = os.environ.get("POLYMAKER_CAPITAL_USDC") or os.environ.get(
        "POLYMAKER_BANKROLL_USDC"
    )
    if raw is None or raw == "":
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, v)
