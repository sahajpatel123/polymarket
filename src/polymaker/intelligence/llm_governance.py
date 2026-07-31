"""LLM governance: hard safeguards around any AI output.

The LLM is a *nudger*, not a *steerer*. It may adjust parameters; it
may not bypass the math, the risk manager, or the strategy engine.

Architecture (three brains, not one):

1. **Math brain (every tick)** — microprice, FV, inventory skew, regime,
   post-only quotes. Final authority on prices and quote sides.
2. **LLM brain (slow loop, 10-30 min)** — ranking, knob nudges,
   narrative, memory. Subject to governance.
3. **Evidence brain (promotion gate)** — paper/shadow apply for
   ``paper_seconds`` before any LLM-suggested knob goes live.

Six hard rules (verified by tests):

1. **Positive allowlist (SAFE_KNOBS).** The LLM may only touch
   parameters in :data:`SAFE_KNOBS`. Anything else is rejected,
   not silently stripped. The allowlist is the *only* knob set;
   the legacy FORBIDDEN list is now a defense-in-depth check.
2. **LLM size multiplier ≤ 0.5.** LLM-influenced sizing can never
   exceed half the deterministic cap. Calibrated-confidence: 0
   → 0 size; 1.0 → 0.5 cap.
3. **Dead-LLM timer.** If an LLM call doesn't return in
   ``dead_llm_timeout_s`` (default 5s), the bot falls back to the
   deterministic strategy. The bot does not go dark waiting.
4. **Daily-loss kill is non-negotiable.** Tracked separately for
   LLM-influenced trades so a hallucination can't be hidden inside
   overall PnL.
5. **LLM reasoning log.** Every LLM decision (prompt, response,
   parsed action, governance result) appends to a JSONL file the
   operator can audit.
6. **No directional bets.** The LLM may suggest parameter nudges
   (spread mult, offset, regime threshold). It may not suggest
   ``side`` flips or "buy this because X". Any action that flips
   the quote side is rejected.

Two additional gates layered on top:

7. **Reward-eligibility gate.** If a market's
   ``rewardsMinSize × price`` exceeds the per-market cap, the LLM
   cannot mark that market as "active" or "selected". Activating
   it would just burn gas on undersized orders.
8. **Paper-promotion gate.** LLM-suggested size/spread deltas are
   applied to a *draft profile* in the :class:`DraftStore` and
   must run paper for ``paper_seconds`` (default 3600) before
   they can be promoted. The engine reads from ``live``; shadow
   reads from ``draft``.

This module is the *only* place these rules live. The agent calls
``governance.check_and_log()`` after every LLM response; nothing
else is allowed to touch the output.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("polymaker.intelligence.llm_governance")


# ── Constants (overridable in env) ────────────────────────────────────

DEFAULT_LLM_SIZE_MULT = 0.5          # hard cap: LLM ≤ 0.5x normal size
DEFAULT_DEAD_LLM_TIMEOUT_S = 5.0     # if LLM takes longer, fall back
DEFAULT_LLM_DAILY_LOSS_PCT = 0.05    # 5% of capital in a day from LLM trades = halt LLM features
DEFAULT_REASONING_LOG_PATH = Path("livecfg/logs/llm_reasoning.jsonl")
DEFAULT_PAPER_SECONDS = 3600         # 1h paper before LLM knob promotion

# Positive allowlist: the LLM may ONLY touch these knobs.
# This matches self_improve.SAFE_IMMEDIATE_KEYS so that
# governance and self-improve agree on the same set of safe knobs.
SAFE_KNOBS: frozenset[str] = frozenset({
    "delta_min_ticks",
    "c_vol",
    "c_tox",
    "c_kyle",
    "gamma",
    "min_edge_ticks",
    "layer_step_ticks",
    "layers",
    "reprice_ticks",
    "resize_frac",
    "flow_fv_weight",
    "trend_flow_z",
    "trend_vol_ratio",
    "event_jump_ticks",
    "event_cooloff_s",
    "event_sweep_mult",
    "event_sweep_frac",
    "join_best_bid",
    "spread_mult",       # governance may cap this to 1.0
    "size_pct",          # governance may cap this to LLM_SIZE_MULT
})

# Defense-in-depth: known-bad parameter names. Anything in this set is
# rejected regardless of SAFE_KNOBS. The positive allowlist is the
# primary check; this is a backstop.
FORBIDDEN_LLM_PARAMS: frozenset[str] = frozenset({
    "signature_type",
    "kill_switch",
    "post_only",
    "risk_profile",
    "bankroll_usdc",
    "POLYMAKER_CAPITAL_USDC",
    "daily_loss_kill_pct",
    "max_drawdown_kill_pct",
    "max_total_exposure_usdc",
    "max_market_notional_usdc",
    "max_event_group_loss_usdc",
    "max_open_orders_per_market",
    "max_position",
    "heartbeat",
    "ws_stale_halt_s",
    "user_ws_blind_halt_s",
    "heartbeat_halt_failures",
    "q_max_usdc",
    "q_soft_frac",
    "base_size_usdc",
    "kelly_fraction",
    "reduce_only_hours",
    "halt_before_hours",
    "merge_min_size",
    "reward_size_mult",
    "use_as_reservation_price",
})

# Forbidden LLM output fields: anything that smells like a direction.
FORBIDDEN_LLM_FIELDS: frozenset[str] = frozenset({
    "side",
    "direction",
    "go_long",
    "go_short",
    "buy_this_market",
    "predict_winner",
    "directional_bet",
})

# Numeric range clamps per knob. The LLM may set these, but the
# value is clamped to the allowed range. Defense in depth against
# prompts that try to "1000x the spread" or similar.
SAFE_KNOB_RANGES: dict[str, tuple[float, float]] = {
    "delta_min_ticks": (0.0, 10.0),
    "c_vol": (0.0, 5.0),
    "c_tox": (0.0, 5.0),
    "c_kyle": (0.0, 5.0),
    "gamma": (0.0, 5.0),
    "min_edge_ticks": (0.0, 20.0),
    "layer_step_ticks": (0.0, 10.0),
    "layers": (1.0, 8.0),
    "reprice_ticks": (0.0, 20.0),
    "resize_frac": (0.0, 1.0),
    "flow_fv_weight": (0.0, 5.0),
    "trend_flow_z": (0.0, 10.0),
    "trend_vol_ratio": (0.0, 20.0),
    "event_jump_ticks": (0.0, 20.0),
    "event_cooloff_s": (0.0, 3600.0),
    "event_sweep_mult": (0.0, 10.0),
    "event_sweep_frac": (0.0, 1.0),
    "join_best_bid": (0.0, 1.0),
    "spread_mult": (0.5, 3.0),       # LLM may not widen more than 3x
    "size_pct": (0.0, 1.0),
}


# ── Decision result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GovernanceDecision:
    """The output of :meth:`LLMGovernance.check_and_log`.

    The caller MUST honor ``approved`` and ``actions``. If
    ``fallback_to_deterministic`` is True, the caller must use
    the deterministic strategy unchanged.

    Fields added in the verdict-driven refactor:

    - ``confidence`` — the LLM's reported confidence in [0, 1].
    - ``size_pct_after_cap`` — ``size_pct`` after the 0.5 hard cap
      and confidence scaling. Engine should use this for sizing.
    - ``paper_required`` — True iff the LLM-suggested knobs must
      run paper for ``paper_seconds`` before going live. The
      ``DraftStore`` handles promotion.
    - ``reward_eligibility`` — None for non-selection calls; a
      ``RewardEligibility`` instance for market-selection calls.
    - ``rejected_keys`` — knobs the LLM proposed that are NOT in
      ``SAFE_KNOBS``. Returned so the operator can audit what
      the LLM wanted.
    - ``clamped_keys`` — knobs the LLM proposed that were clamped
      to their allowed numeric range.
    """

    approved: bool
    actions: dict[str, Any]              # sanitized parameter nudges
    stripped_keys: tuple[str, ...]       # forbidden keys removed (defense-in-depth)
    stripped_fields: tuple[str, ...]     # forbidden direction fields removed
    rejected_keys: tuple[str, ...]       # keys not in SAFE_KNOBS
    clamped_keys: tuple[str, ...]        # keys clamped to SAFE_KNOB_RANGES
    rejection_reason: str = ""
    fallback_to_deterministic: bool = False
    reasoning_id: int = 0
    latency_ms: float = 0.0
    confidence: float = 0.0
    size_pct_after_cap: float = 0.0
    paper_required: bool = False
    reward_eligibility: RewardEligibility | None = None


# ── Daily LLM loss tracker ────────────────────────────────────────────


@dataclass
class LLMDailyLoss:
    """Tracks PnL attributable to LLM-influenced trades.

    Distinct from total PnL so a single hallucination series can't be
    hidden inside an otherwise profitable day.
    """

    capital_usdc: float
    day_start_ts: float = field(default_factory=time.time)
    day_pnl_usdc: float = 0.0
    n_fills: int = 0
    halted: bool = False
    halt_reason: str = ""
    _kill_pct: float = DEFAULT_LLM_DAILY_LOSS_PCT

    def set_kill_pct(self, pct: float) -> None:
        if 0 < pct < 1:
            self._kill_pct = pct

    def record_fill(self, pnl_usdc: float) -> None:
        if self.halted:
            return
        self.day_pnl_usdc += pnl_usdc
        self.n_fills += 1
        threshold = self.capital_usdc * self._kill_pct
        if self.day_pnl_usdc <= -threshold:
            self.halted = True
            self.halt_reason = (
                f"llm_daily_loss {self.day_pnl_usdc:.2f} ≤ "
                f"-{threshold:.2f} ({self._kill_pct:.0%} of capital)"
            )

    def reset(self) -> None:
        self.day_pnl_usdc = 0.0
        self.n_fills = 0
        self.halted = False
        self.halt_reason = ""
        self.day_start_ts = time.time()


# ── Reward eligibility ───────────────────────────────────────────────


@dataclass(frozen=True)
class RewardEligibility:
    """Per-market reward-eligibility check for LLM market selection.

    A market is reward-eligible iff a single order at the typical
    price can fund ``rewardsMinSize`` shares. If it can't, the LLM
    must not mark the market as "active" or "selected" — even
    perfect in-band quoting earns $0 reward when orders are
    undersized.
    """

    condition_id: str
    rewards_min_size: float
    typical_price: float
    per_market_cap_usdc: float
    eligible: bool
    min_order_notional_usdc: float
    shortfall_usdc: float

    @classmethod
    def check(
        cls,
        condition_id: str,
        rewards_min_size: float,
        typical_price: float,
        per_market_cap_usdc: float,
    ) -> RewardEligibility:
        """Compute whether the per-market cap can fund a reward-min order.

        A market where the cap is below ``rewardsMinSize × price`` is
        unprofitable: orders would be undersized for rewards, and
        the LLM is forbidden from selecting it.
        """
        min_notional = max(0.0, rewards_min_size) * max(0.0, typical_price)
        eligible = per_market_cap_usdc >= min_notional
        shortfall = max(0.0, min_notional - per_market_cap_usdc)
        return cls(
            condition_id=condition_id,
            rewards_min_size=rewards_min_size,
            typical_price=typical_price,
            per_market_cap_usdc=per_market_cap_usdc,
            eligible=eligible,
            min_order_notional_usdc=min_notional,
            shortfall_usdc=shortfall,
        )


# ── Main governance class ─────────────────────────────────────────────


class LLMGovernance:
    """The single gatekeeper between LLM output and the engine.

    Lifecycle:
        gov = LLMGovernance(capital_usdc=500, log_path=Path("..."))
        decision = gov.check_and_log(prompt, response, llm_started_at)
        if decision.approved:
            apply_to_engine(decision.actions)
        elif decision.fallback_to_deterministic:
            use_deterministic_strategy()
    """

    def __init__(
        self,
        capital_usdc: float,
        log_path: Path | str = DEFAULT_REASONING_LOG_PATH,
        llm_size_mult: float = DEFAULT_LLM_SIZE_MULT,
        dead_llm_timeout_s: float = DEFAULT_DEAD_LLM_TIMEOUT_S,
        paper_seconds: float = DEFAULT_PAPER_SECONDS,
    ) -> None:
        if not 0 < llm_size_mult <= 1.0:
            raise ValueError(f"llm_size_mult must be in (0, 1]; got {llm_size_mult}")
        if dead_llm_timeout_s <= 0:
            raise ValueError(f"dead_llm_timeout_s must be > 0; got {dead_llm_timeout_s}")
        if paper_seconds < 0:
            raise ValueError(f"paper_seconds must be >= 0; got {paper_seconds}")
        self.capital_usdc = capital_usdc
        self.log_path = Path(log_path)
        self.llm_size_mult = llm_size_mult
        self.dead_llm_timeout_s = dead_llm_timeout_s
        self.paper_seconds = paper_seconds
        self.daily_loss = LLMDailyLoss(capital_usdc=capital_usdc)
        self._counter = 0
        self._init_log()

    def _init_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.touch()

    # ── Public API ─────────────────────────────────────────────────

    def check_and_log(
        self,
        prompt: str,
        response: Any,
        llm_started_at: float,
        *,
        context: dict[str, Any] | None = None,
        confidence: float = 0.0,
    ) -> GovernanceDecision:
        """Validate an LLM response and emit a governance decision.

        Parameters
        ----------
        prompt
            The prompt sent to the LLM (logged for audit).
        response
            The LLM's response. May be a dict, str, or list. We try
            to extract ``actions`` / ``profile_overrides`` / ``parameters``
            from it.
        llm_started_at
            Unix timestamp when the LLM call started. Used to compute
            latency and trigger the dead-LLM fallback.
        context
            Optional metadata for the log row (cid, regime, etc.).
        confidence
            LLM-reported confidence in [0, 1]. Used for calibrated
            sizing: ``size_pct = llm_size_mult * confidence`` capped
            to ``llm_size_mult``. A confidence of 0 → size 0.

        Returns
        -------
        GovernanceDecision
            The verdict. ``approved=True`` means the engine may apply
            ``decision.actions`` (subject to ``paper_required`` —
            the caller should pass these to the DraftStore and run
            paper for ``paper_seconds`` before promoting).
        """
        latency_ms = (time.time() - llm_started_at) * 1000.0
        timeout_ms = self.dead_llm_timeout_s * 1000.0
        confidence = max(0.0, min(1.0, float(confidence)))

        # ── Rule 3: dead-LLM timer ────────────────────────────────
        if latency_ms > timeout_ms:
            decision = self._make_reject(
                prompt=prompt,
                response=response,
                context=context,
                latency_ms=latency_ms,
                rejection_reason=(
                    f"dead_llm_timeout latency={latency_ms:.0f}ms "
                    f"> {timeout_ms:.0f}ms"
                ),
                fallback_to_deterministic=True,
            )
            return decision

        # ── Rule 6: no directional bets ──────────────────────────
        actions_raw = self._extract_actions(response)
        direction_attempt = self._direction_attempt(actions_raw)
        if direction_attempt:
            decision = self._make_reject(
                prompt=prompt,
                response=response,
                context=context,
                latency_ms=latency_ms,
                stripped_fields=direction_attempt,
                rejection_reason=(
                    f"directional_bet_forbidden: {direction_attempt}"
                ),
            )
            return decision

        # Now strip remaining direction fields (defense in depth).
        actions, stripped_fields = self._strip_direction(actions_raw)

        # ── Rule 1 (positive allowlist): keep only SAFE_KNOBS ─────
        allowlisted, rejected_keys = self._allowlist(actions)
        # Defense in depth: if any FORBIDDEN_LLM_PARAMS snuck in, reject.
        forbidden_present, forbidden_keys = self._find_forbidden(allowlisted)
        if forbidden_present:
            decision = self._make_reject(
                prompt=prompt,
                response=response,
                context=context,
                latency_ms=latency_ms,
                stripped_keys=tuple(forbidden_keys),
                rejection_reason=(
                    f"llm_attempted_forbidden_param: {forbidden_keys}"
                ),
            )
            return decision

        # ── Clamp numeric values to SAFE_KNOB_RANGES ─────────────
        clamped, clamped_keys = self._clamp_ranges(allowlisted)

        # ── Rule 2: calibrated size cap ──────────────────────────
        # size_pct_after_cap = llm_size_mult * confidence (0..llm_size_mult)
        size_pct_after_cap = self.llm_size_mult * confidence
        if "size_pct" in clamped:
            try:
                requested = float(clamped["size_pct"])
            except (TypeError, ValueError):
                clamped.pop("size_pct", None)
                size_pct_after_cap = 0.0
            else:
                # Take the MIN of (LLM-requested, confidence-scaled cap).
                size_pct_after_cap = min(requested, self.llm_size_mult, self.llm_size_mult * confidence)
                clamped["size_pct"] = size_pct_after_cap
        if "spread_mult" in clamped:
            try:
                v = float(clamped["spread_mult"])
                # Hard cap: 0.5..3.0 already applied by clamp_ranges.
                clamped["spread_mult"] = v
            except (TypeError, ValueError):
                clamped.pop("spread_mult", None)

        # ── Rule 4: daily LLM loss ────────────────────────────────
        if self.daily_loss.halted:
            decision = self._make_reject(
                prompt=prompt,
                response=response,
                context=context,
                latency_ms=latency_ms,
                rejection_reason=(
                    f"llm_daily_loss_halt: {self.daily_loss.halt_reason}"
                ),
            )
            return decision

        # ── Reward eligibility (only when this is a selection call) ─
        eligibility: RewardEligibility | None = None
        if context and context.get("kind") == "market_selection":
            cid = str(context.get("condition_id", ""))
            eligibility = RewardEligibility.check(
                condition_id=cid,
                rewards_min_size=float(context.get("rewards_min_size", 0.0) or 0.0),
                typical_price=float(context.get("typical_price", 0.0) or 0.0),
                per_market_cap_usdc=float(context.get("per_market_cap_usdc", 0.0) or 0.0),
            )
            if not eligibility.eligible:
                decision = self._make_reject(
                    prompt=prompt,
                    response=response,
                    context=context,
                    latency_ms=latency_ms,
                    rejection_reason=(
                        f"market_not_reward_eligible: "
                        f"shortfall=${eligibility.shortfall_usdc:.2f} "
                        f"(need ${eligibility.min_order_notional_usdc:.2f}, "
                        f"have ${eligibility.per_market_cap_usdc:.2f})"
                    ),
                )
                # Attach the eligibility so the operator can see why.
                return self._attach_eligibility(decision, eligibility)

        # ── Rule 8: paper-promotion gate ─────────────────────────
        # Any LLM-suggested knob is paper_required=True; the caller
        # routes it to DraftStore and runs paper for paper_seconds
        # before promoting to live.
        paper_required = bool(clamped) and self.paper_seconds > 0

        decision = GovernanceDecision(
            approved=True,
            actions=clamped,
            stripped_keys=tuple(forbidden_keys),
            stripped_fields=tuple(stripped_fields),
            rejected_keys=tuple(rejected_keys),
            clamped_keys=tuple(clamped_keys),
            rejection_reason="",
            fallback_to_deterministic=False,
            reasoning_id=self._next_id(),
            latency_ms=latency_ms,
            confidence=confidence,
            size_pct_after_cap=size_pct_after_cap,
            paper_required=paper_required,
            reward_eligibility=eligibility,
        )
        self._write_log(prompt, response, decision, context)
        return decision

    def _make_reject(
        self,
        *,
        prompt: str,
        response: Any,
        context: dict[str, Any] | None,
        latency_ms: float,
        rejection_reason: str,
        stripped_keys: tuple[str, ...] = (),
        stripped_fields: tuple[str, ...] = (),
        fallback_to_deterministic: bool = False,
    ) -> GovernanceDecision:
        decision = GovernanceDecision(
            approved=False,
            actions={},
            stripped_keys=stripped_keys,
            stripped_fields=stripped_fields,
            rejected_keys=(),
            clamped_keys=(),
            rejection_reason=rejection_reason,
            fallback_to_deterministic=fallback_to_deterministic,
            reasoning_id=self._next_id(),
            latency_ms=latency_ms,
        )
        self._write_log(prompt, response, decision, context)
        return decision

    def _attach_eligibility(
        self, decision: GovernanceDecision, eligibility: RewardEligibility
    ) -> GovernanceDecision:
        """Build a new decision with the eligibility field populated."""
        from dataclasses import replace
        return replace(decision, reward_eligibility=eligibility)

    def record_llm_fill(self, pnl_usdc: float) -> None:
        """Update LLM-day PnL after a fill attributable to LLM influence."""
        self.daily_loss.record_fill(pnl_usdc)

    def critique_prompt(
        self,
        *,
        suggestion: str,
        actions: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> str:
        """Build an adversarial-critic prompt for the LLM to red-team itself.

        Per the verdict: "Second prompt: 'attack this suggestion; find
        failure modes' — still cannot override risk". This returns a
        prompt the caller can pass to Grok 4.5 to ask the model to
        attack its own idea. The critique response is *advisory only*;
        it cannot override governance.
        """
        # Self-contained template (kept inline so the governance module
        # has no hard dependency on the prompts registry).
        return (
            "You are the adversary. Find failure modes in this bot suggestion "
            "before it goes live. You are ADVISORY ONLY — you CANNOT modify "
            "risk caps, change side, or override governance. The risk manager "
            "is final. You only return JSON describing what you found.\n\n"
            "Respond as JSON: {failure_modes: [str], severity: 0..1, "
            "should_reject: bool, reasoning: str}.\n\n"
            "Severity: 0=harmless, 0.5=concerning, 1.0=will likely lose money.\n"
            "should_reject: true iff severity >= 0.7 OR a directional bet in disguise.\n\n"
            "The bot's risk policy (NON-NEGOTIABLE):\n"
            "- LLM may only touch SAFE_KNOBS: spread_mult, size_pct, c_tox, c_vol, "
            "layers, event_cooloff_s, event_sweep_mult, etc.\n"
            "- LLM may NOT touch: daily_loss_kill_pct, max_position, signature_type, "
            "post_only, bankroll_usdc, heartbeat, ws_stale_halt_s.\n"
            "- LLM size_pct is capped at 0.5 (and confidence-scaled).\n"
            "- LLM may NOT suggest side, direction, or 'buy this market'.\n\n"
            f"Suggestion: {suggestion}\n"
            f"Actions: {actions}\n"
            f"Context: {context or {}}\n"
        )

    # ── Internals ─────────────────────────────────────────────────

    def _strip_forbidden_params(
        self, actions: dict[str, Any] | None
    ) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(actions, dict):
            return {}, []
        clean: dict[str, Any] = {}
        stripped: list[str] = []
        for k, v in actions.items():
            if k in FORBIDDEN_LLM_PARAMS:
                stripped.append(k)
            else:
                clean[k] = v
        return clean, stripped

    def _strip_direction(
        self, actions: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Strip any field that smells like a direction call."""
        if not isinstance(actions, dict):
            return {}, []
        clean: dict[str, Any] = {}
        stripped: list[str] = []
        for k, v in actions.items():
            if k in FORBIDDEN_LLM_FIELDS:
                stripped.append(k)
            else:
                clean[k] = v
        return clean, stripped

    def _allowlist(
        self, actions: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Positive allowlist: keep only SAFE_KNOBS, reject the rest.

        The positive allowlist is the primary rule (per the verdict).
        The defense-in-depth ``_find_forbidden`` check below is a
        backstop in case ``SAFE_KNOBS`` is ever loosened.
        """
        if not isinstance(actions, dict):
            return {}, []
        clean: dict[str, Any] = {}
        rejected: list[str] = []
        for k, v in actions.items():
            if k in SAFE_KNOBS:
                clean[k] = v
            else:
                rejected.append(k)
        return clean, rejected

    def _find_forbidden(
        self, actions: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """Defense-in-depth: any FORBIDDEN_LLM_PARAMS triggers reject."""
        if not isinstance(actions, dict):
            return False, []
        found = [k for k in actions if k in FORBIDDEN_LLM_PARAMS]
        return bool(found), found

    def _clamp_ranges(
        self, actions: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        """Clamp numeric values to SAFE_KNOB_RANGES.

        Non-numeric values for numeric knobs are dropped. Unknown
        knobs are passed through unchanged (the allowlist already
        vetted them).
        """
        if not isinstance(actions, dict):
            return {}, []
        clean: dict[str, Any] = {}
        clamped: list[str] = []
        for k, v in actions.items():
            if k in SAFE_KNOB_RANGES:
                lo, hi = SAFE_KNOB_RANGES[k]
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    clamped.append(k)
                    continue
                clamped_v = max(lo, min(hi, n))
                if clamped_v != n:
                    clamped.append(k)
                clean[k] = clamped_v
            else:
                clean[k] = v
        return clean, clamped

    def _extract_actions(self, response: Any) -> dict[str, Any]:
        """Pull the action dict out of the LLM response."""
        if isinstance(response, dict):
            actions = (
                response.get("actions")
                or response.get("profile_overrides")
                or response.get("parameters")
                or {}
            )
            if isinstance(actions, dict):
                return actions
        return {}

    def _direction_attempt(self, actions: dict[str, Any]) -> tuple[str, ...]:
        """Return the tuple of forbidden-direction keys present, or ().

        Used to *reject* the call rather than silently strip.
        """
        if not isinstance(actions, dict):
            return ()
        found = tuple(k for k in actions if k in FORBIDDEN_LLM_FIELDS)
        return found

    def _write_log(
        self,
        prompt: str,
        response: Any,
        decision: GovernanceDecision,
        context: dict[str, Any] | None,
    ) -> None:
        row = {
            "ts": time.time(),
            "reasoning_id": decision.reasoning_id,
            "latency_ms": round(decision.latency_ms, 2),
            "approved": decision.approved,
            "fallback_to_deterministic": decision.fallback_to_deterministic,
            "rejection_reason": decision.rejection_reason,
            "actions": decision.actions,
            "stripped_keys": list(decision.stripped_keys),
            "stripped_fields": list(decision.stripped_fields),
            "rejected_keys": list(decision.rejected_keys),
            "clamped_keys": list(decision.clamped_keys),
            "confidence": decision.confidence,
            "size_pct_after_cap": decision.size_pct_after_cap,
            "paper_required": decision.paper_required,
            "paper_seconds": self.paper_seconds,
            "llm_size_mult": self.llm_size_mult,
            "dead_llm_timeout_s": self.dead_llm_timeout_s,
            "llm_daily_pnl_usdc": self.daily_loss.day_pnl_usdc,
            "llm_daily_halted": self.daily_loss.halted,
            "reward_eligibility": (
                {
                    "condition_id": decision.reward_eligibility.condition_id,
                    "eligible": decision.reward_eligibility.eligible,
                    "min_order_notional_usdc": decision.reward_eligibility.min_order_notional_usdc,
                    "shortfall_usdc": decision.reward_eligibility.shortfall_usdc,
                }
                if decision.reward_eligibility
                else None
            ),
            "context": context or {},
            # Truncate prompt/response to keep the log readable.
            "prompt": prompt[:2000],
            "response": str(response)[:2000],
        }
        try:
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except OSError as exc:
            log.warning("failed to write LLM reasoning log: %s", exc)

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter
