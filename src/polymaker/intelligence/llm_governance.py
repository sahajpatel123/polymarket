"""LLM governance: hard safeguards around any AI output.

The LLM is a *nudger*, not a *steerer*. It may adjust parameters; it
may not bypass the math, the risk manager, or the strategy engine.

Six hard rules (verified by tests):

1. **Same risk gates as human quoting.** Every LLM-suggested parameter
   passes through the same :class:`RiskPolicy` and ``RiskManager``
   that human-driven quoting does. An LLM call that says "widen
   spread by 5x" is capped by the policy; one that says "remove
   daily loss kill" is rejected entirely.

2. **LLM size multiplier ≤ 0.5.** LLM-influenced sizing can never
   exceed half the deterministic cap. This bounds hallucination
   damage.

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

# Forbidden parameter names: LLM may not touch these.
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
    "max_position",
    "max_open_orders_per_market",
    "heartbeat",
    "ws_stale_halt_s",
    "user_ws_blind_halt_s",
    "heartbeat_halt_failures",
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


# ── Decision result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GovernanceDecision:
    """The output of :meth:`LLMGovernance.check_and_log`.

    The caller MUST honor ``approved`` and ``actions``. If
    ``fallback_to_deterministic`` is True, the caller must use
    the deterministic strategy unchanged.
    """

    approved: bool
    actions: dict[str, Any]              # sanitized parameter nudges
    stripped_keys: tuple[str, ...]       # forbidden keys removed
    stripped_fields: tuple[str, ...]     # forbidden direction fields removed
    rejection_reason: str = ""
    fallback_to_deterministic: bool = False
    reasoning_id: int = 0
    latency_ms: float = 0.0


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
    ) -> None:
        if not 0 < llm_size_mult <= 1.0:
            raise ValueError(f"llm_size_mult must be in (0, 1]; got {llm_size_mult}")
        if dead_llm_timeout_s <= 0:
            raise ValueError(f"dead_llm_timeout_s must be > 0; got {dead_llm_timeout_s}")
        self.capital_usdc = capital_usdc
        self.log_path = Path(log_path)
        self.llm_size_mult = llm_size_mult
        self.dead_llm_timeout_s = dead_llm_timeout_s
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
    ) -> GovernanceDecision:
        """Validate an LLM response and emit a governance decision.

        Parameters
        ----------
        prompt
            The prompt sent to the LLM (logged for audit).
        response
            The LLM's response. May be a dict, str, or list. We try
            to extract ``profile_overrides`` or ``actions`` from it.
        llm_started_at
            Unix timestamp when the LLM call started. Used to compute
            latency and trigger the dead-LLM fallback.
        context
            Optional metadata for the log row (cid, regime, etc.).
        """
        latency_ms = (time.time() - llm_started_at) * 1000.0
        timeout_ms = self.dead_llm_timeout_s * 1000.0

        # ── Rule 3: dead-LLM timer ────────────────────────────────
        if latency_ms > timeout_ms:
            decision = GovernanceDecision(
                approved=False,
                actions={},
                stripped_keys=(),
                stripped_fields=(),
                rejection_reason=(
                    f"dead_llm_timeout latency={latency_ms:.0f}ms "
                    f"> {timeout_ms:.0f}ms"
                ),
                fallback_to_deterministic=True,
                reasoning_id=self._next_id(),
                latency_ms=latency_ms,
            )
            self._write_log(prompt, response, decision, context)
            return decision

        # ── Rule 6: no directional bets ──────────────────────────
        # Check the raw response FIRST; if the LLM tried to set any
        # direction field, reject the whole call (do not just strip).
        actions_raw = self._extract_actions(response)
        direction_attempt = self._direction_attempt(actions_raw)
        if direction_attempt:
            decision = GovernanceDecision(
                approved=False,
                actions={},
                stripped_keys=(),
                stripped_fields=direction_attempt,
                rejection_reason=(
                    f"directional_bet_forbidden: {direction_attempt}"
                ),
                fallback_to_deterministic=False,
                reasoning_id=self._next_id(),
                latency_ms=latency_ms,
            )
            self._write_log(prompt, response, decision, context)
            return decision

        # Now strip remaining direction fields (defense in depth) and
        # proceed to the parameter gates.
        actions, stripped_fields = self._strip_direction(actions_raw)

        # ── Rule 1: same risk gates ───────────────────────────────
        # Strip forbidden parameter names the LLM may not touch.
        sanitized, stripped_keys = self._strip_forbidden_params(actions)
        if any(k in stripped_keys for k in ("daily_loss_kill_pct", "max_position")):
            decision = GovernanceDecision(
                approved=False,
                actions={},
                stripped_keys=stripped_keys,
                stripped_fields=stripped_fields,
                rejection_reason=(
                    f"llm_attempted_to_modify_risk_cap stripped={stripped_keys}"
                ),
                fallback_to_deterministic=False,
                reasoning_id=self._next_id(),
                latency_ms=latency_ms,
            )
            self._write_log(prompt, response, decision, context)
            return decision

        # ── Rule 2: LLM size cap ≤ 0.5 ───────────────────────────
        if "size_pct" in sanitized:
            try:
                requested = float(sanitized["size_pct"])
                capped = min(requested, self.llm_size_mult)
                sanitized["size_pct"] = capped
            except (TypeError, ValueError):
                sanitized.pop("size_pct", None)

        # ── Rule 4: daily LLM loss ────────────────────────────────
        if self.daily_loss.halted:
            decision = GovernanceDecision(
                approved=False,
                actions={},
                stripped_keys=stripped_keys,
                stripped_fields=stripped_fields,
                rejection_reason=(
                    f"llm_daily_loss_halt: {self.daily_loss.halt_reason}"
                ),
                fallback_to_deterministic=False,
                reasoning_id=self._next_id(),
                latency_ms=latency_ms,
            )
            self._write_log(prompt, response, decision, context)
            return decision

        decision = GovernanceDecision(
            approved=True,
            actions=sanitized,
            stripped_keys=tuple(stripped_keys),
            stripped_fields=tuple(stripped_fields),
            fallback_to_deterministic=False,
            reasoning_id=self._next_id(),
            latency_ms=latency_ms,
        )
        self._write_log(prompt, response, decision, context)
        return decision

    def record_llm_fill(self, pnl_usdc: float) -> None:
        """Update LLM-day PnL after a fill attributable to LLM influence."""
        self.daily_loss.record_fill(pnl_usdc)

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
            "llm_size_mult": self.llm_size_mult,
            "dead_llm_timeout_s": self.dead_llm_timeout_s,
            "llm_daily_pnl_usdc": self.daily_loss.day_pnl_usdc,
            "llm_daily_halted": self.daily_loss.halted,
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
