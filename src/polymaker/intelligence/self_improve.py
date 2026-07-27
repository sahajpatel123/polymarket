"""Self-improvement loop: decay/hit-rate gate → Grok reasoning → draft/promote.

Profile mutations never touch risk caps / daily-loss / max position. Non-risk
tweaks (spread / regime thresholds) may apply immediately when the LLM says
``paper_validation_required=false``; everything else goes draft → paper →
promote or reject.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from polymaker.intelligence.profile_history import ProfileHistory
from polymaker.intelligence.self_eval import SelfEvaluation

# Grok 4.5 reasoning model — do not downgrade to a non-reasoning variant.
REASONING_MODEL = "grok-4-1-fast-reasoning"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"

# Immediate-apply allowlist (spread / regime thresholds only).
SAFE_IMMEDIATE_KEYS: frozenset[str] = frozenset(
    {
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
    }
)

# Never allow these via self-improve (risk / capital / kill).
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "q_max_usdc",
        "q_soft_frac",
        "base_size_usdc",
        "bankroll_usdc",
        "kelly_fraction",
        "max_open_orders_per_market",
        "reduce_only_hours",
        "halt_before_hours",
        "merge_min_size",
        "reward_size_mult",
    }
)

HIT_RATE_TRIGGER = 0.4
MIN_TRADES_FOR_HIT_RATE = 50
DEFAULT_PAPER_SECONDS = 3600


class MemoryLike(Protocol):
    def add(self, text: str, *, tags: list[str] | None = None) -> Any: ...

    def recent(self, n: int = 20) -> list[Any]: ...


@dataclass
class TradeJournalEntry:
    """One trade / decision row for the LLM journal prompt."""

    pnl: float
    regime: str = "QUIET"
    spread: float = 0.0
    fill_rate: float = 0.0
    markout: float = 0.0
    offset: str = ""


@dataclass
class ImprovementSuggestion:
    diagnosis: str
    suggestion: str
    expected_impact_pct: float
    paper_validation_required: bool
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_llm(cls, data: dict[str, Any]) -> ImprovementSuggestion:
        overrides = data.get("profile_overrides") or data.get("overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}
        return cls(
            diagnosis=str(data.get("diagnosis", "")),
            suggestion=str(data.get("suggestion", "")),
            expected_impact_pct=float(data.get("expected_impact_pct", 0.0) or 0.0),
            paper_validation_required=bool(
                data.get("paper_validation_required", True)
            ),
            profile_overrides={str(k): v for k, v in overrides.items()},
            raw=dict(data),
        )


@dataclass
class ImproveResult:
    triggered: bool
    reason: str
    suggestion: ImprovementSuggestion | None = None
    applied: bool = False
    promoted: bool = False
    rejected: bool = False
    paper_validated: bool = False
    draft_profile: dict[str, Any] | None = None
    live_profile: dict[str, Any] | None = None
    history_id: int | None = None


def needs_improvement(evaluation: SelfEvaluation) -> tuple[bool, str]:
    """Return (should_run, reason) from SelfEvaluation state."""
    if evaluation.decay.is_decaying():
        return True, "strategy_decaying"
    n = evaluation.attribution.n_decisions
    hr = evaluation.attribution.hit_rate()
    if n >= MIN_TRADES_FOR_HIT_RATE and hr < HIT_RATE_TRIGGER:
        return True, f"hit_rate={hr:.3f}<{HIT_RATE_TRIGGER} over {n} trades"
    return False, "healthy"


def build_trade_journal(
    evaluation: SelfEvaluation,
    *,
    extras: list[TradeJournalEntry] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Build a journal of up to ``limit`` recent trades for the LLM prompt."""
    rows: list[dict[str, Any]] = []
    if extras:
        for e in extras[-limit:]:
            rows.append(asdict(e))
    # Attribution decisions are the primary source when extras are sparse.
    remaining = limit - len(rows)
    if remaining > 0:
        decisions = list(evaluation.attribution.decisions)[-remaining:]
        fill_rate = evaluation.calibration.fill_rate()
        avg_as = evaluation.calibration.avg_as()
        for d in decisions:
            rows.append(
                {
                    "pnl": float(d.get("pnl", 0.0)),
                    "regime": str(d.get("regime", "")),
                    "spread": 0.0,
                    "fill_rate": fill_rate,
                    "markout": avg_as,
                    "offset": str(d.get("offset", "")),
                }
            )
    return rows[-limit:]


def _strip_forbidden(overrides: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in overrides.items() if k not in FORBIDDEN_KEYS}


def requires_paper(overrides: dict[str, Any], llm_flag: bool) -> bool:
    """Force paper validation unless every override is on the safe allowlist."""
    if not overrides:
        return True
    if any(k not in SAFE_IMMEDIATE_KEYS for k in overrides):
        return True
    return bool(llm_flag)


def apply_overrides(
    profile: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """Return a shallow-copied profile with safe overrides applied."""
    out = dict(profile)
    for k, v in _strip_forbidden(overrides).items():
        out[k] = v
    return out


def call_grok_reasoning(
    *,
    system: str,
    user: str,
    api_key: str | None = None,
    model: str = REASONING_MODEL,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Call xAI chat completions with the reasoning model; return parsed JSON.

    Reads ``XAI_API_KEY`` from the environment when ``api_key`` is omitted.
    Never embeds a real key in source.
    """
    key = api_key if api_key is not None else os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY not set in environment / .env")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        XAI_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"xAI HTTP {exc.code}: {detail}") from exc

    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        # Some APIs return content parts; join text.
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return json.loads(content)


def _memory_snippets(memory: MemoryLike | None, n: int = 10) -> list[str]:
    if memory is None:
        return []
    try:
        items = memory.recent(n)
    except Exception:
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            out.append(str(it.get("text") or it.get("content") or it))
        else:
            text = getattr(it, "text", None) or getattr(it, "content", None)
            out.append(str(text if text is not None else it))
    return out


IMPROVE_SYSTEM = (
    "You are Polymaker's self-improvement analyst. "
    "Given a trade journal and memory, return JSON only with keys: "
    "diagnosis (str), suggestion (str), expected_impact_pct (float), "
    "paper_validation_required (bool), profile_overrides (object of "
    "StrategyProfile field → new value). "
    "Never propose changes to risk caps, daily loss, q_max_usdc, "
    "base_size_usdc, or bankroll. Prefer spread/regime-threshold tweaks."
)


class SelfImprover:
    """Orchestrates decay detection → LLM suggestion → draft/paper/promote."""

    def __init__(
        self,
        *,
        history: ProfileHistory,
        llm: Callable[..., dict[str, Any]] | None = None,
        memory: MemoryLike | None = None,
        paper_validator: Callable[[dict[str, Any]], bool] | None = None,
        paper_duration_s: float = DEFAULT_PAPER_SECONDS,
        profile_name: str = "default",
    ) -> None:
        self.history = history
        self.llm = llm or call_grok_reasoning
        self.memory = memory
        self.paper_validator = paper_validator
        self.paper_duration_s = paper_duration_s
        self.profile_name = profile_name
        self.draft_profile: dict[str, Any] | None = None
        self.live_profile: dict[str, Any] = {}

    def set_live_profile(self, profile: dict[str, Any]) -> None:
        self.live_profile = dict(profile)

    def run(
        self,
        evaluation: SelfEvaluation,
        *,
        journal_extras: list[TradeJournalEntry] | None = None,
        force: bool = False,
        api_key: str | None = None,
    ) -> ImproveResult:
        """Run one improvement cycle. No-op when healthy unless ``force``."""
        triggered, reason = needs_improvement(evaluation)
        if not triggered and not force:
            return ImproveResult(triggered=False, reason=reason)

        if force and not triggered:
            reason = f"forced ({reason})"

        journal = build_trade_journal(evaluation, extras=journal_extras)
        mem = _memory_snippets(self.memory)
        user = json.dumps(
            {
                "trigger": reason,
                "summary": evaluation.summary(),
                "journal": journal,
                "memory": mem,
                "current_profile": self.live_profile,
            },
            default=str,
        )
        raw = self.llm(system=IMPROVE_SYSTEM, user=user, api_key=api_key)
        suggestion = ImprovementSuggestion.from_llm(raw)
        overrides = _strip_forbidden(suggestion.profile_overrides)
        suggestion.profile_overrides = overrides

        if not overrides:
            return ImproveResult(
                triggered=True,
                reason=reason,
                suggestion=suggestion,
                applied=False,
                live_profile=dict(self.live_profile),
            )

        draft = apply_overrides(self.live_profile, overrides)
        self.draft_profile = draft
        need_paper = requires_paper(overrides, suggestion.paper_validation_required)

        if need_paper:
            return self._paper_then_decide(
                reason=reason,
                suggestion=suggestion,
                draft=draft,
            )

        # Immediate apply (safe spread / regime only).
        hid = self.history.append(
            old_profile=self.live_profile,
            new_profile=draft,
            source="self_improve",
            reason=suggestion.suggestion or suggestion.diagnosis,
            paper_validated=False,
            profile_name=self.profile_name,
        )
        self.live_profile = draft
        self.draft_profile = None
        return ImproveResult(
            triggered=True,
            reason=reason,
            suggestion=suggestion,
            applied=True,
            promoted=True,
            paper_validated=False,
            draft_profile=None,
            live_profile=dict(self.live_profile),
            history_id=hid,
        )

    def _paper_then_decide(
        self,
        *,
        reason: str,
        suggestion: ImprovementSuggestion,
        draft: dict[str, Any],
    ) -> ImproveResult:
        """Apply draft to paper side-by-side, then promote or reject."""
        ok = False
        if self.paper_validator is not None:
            ok = bool(self.paper_validator(draft))
        else:
            # Default: record draft intent; operator/engine runs paper window.
            # Without a validator we do not promote automatically.
            ok = False

        if ok:
            hid = self.history.append(
                old_profile=self.live_profile,
                new_profile=draft,
                source="self_improve",
                reason=(
                    f"[paper-ok {self.paper_duration_s:.0f}s] "
                    f"{suggestion.suggestion or suggestion.diagnosis}"
                ),
                paper_validated=True,
                profile_name=self.profile_name,
            )
            self.live_profile = draft
            self.draft_profile = None
            return ImproveResult(
                triggered=True,
                reason=reason,
                suggestion=suggestion,
                applied=True,
                promoted=True,
                paper_validated=True,
                draft_profile=None,
                live_profile=dict(self.live_profile),
                history_id=hid,
            )

        # Reject: keep live, log draft attempt as no-op history note via reason.
        hid = self.history.append(
            old_profile=self.live_profile,
            new_profile=self.live_profile,
            source="self_improve",
            reason=(
                f"[paper-reject] {suggestion.suggestion or suggestion.diagnosis}"
            ),
            paper_validated=False,
            profile_name=self.profile_name,
        )
        self.draft_profile = draft
        return ImproveResult(
            triggered=True,
            reason=reason,
            suggestion=suggestion,
            applied=False,
            promoted=False,
            rejected=True,
            paper_validated=False,
            draft_profile=dict(draft),
            live_profile=dict(self.live_profile),
            history_id=hid,
        )


def run_improve_cycle(
    evaluation: SelfEvaluation,
    live_profile: dict[str, Any],
    *,
    db_path: str,
    force: bool = False,
    llm: Callable[..., dict[str, Any]] | None = None,
    paper_validator: Callable[[dict[str, Any]], bool] | None = None,
    memory: MemoryLike | None = None,
    profile_name: str = "default",
) -> ImproveResult:
    """Convenience entry used by the CLI."""
    history = ProfileHistory(db_path)
    try:
        improver = SelfImprover(
            history=history,
            llm=llm,
            memory=memory,
            paper_validator=paper_validator,
            profile_name=profile_name,
        )
        improver.set_live_profile(live_profile)
        return improver.run(evaluation, force=force)
    finally:
        history.close()


# Re-export time for tests that patch wall clock in paper windows.
_now = time.time
