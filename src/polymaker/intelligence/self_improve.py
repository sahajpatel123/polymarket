"""Self-improvement loop: decay/hit-rate gate → Grok reasoning → draft/promote.

Profile mutations never touch risk caps / daily-loss / max position. Non-risk
tweaks (spread / regime thresholds) may apply immediately when the LLM says
``paper_validation_required=false``; everything else goes draft → paper →
promote or reject.

LLM calls use ``grok-4-1-fast-reasoning`` only (never a non-reasoning SKU).
``XAI_API_KEY`` is read from the environment — never embedded in source.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from polymaker.intelligence.profile_history import ProfileHistory
from polymaker.intelligence.self_eval import SelfEvaluation

log = logging.getLogger("polymaker.intelligence.self_improve")

REASONING_MODEL = "grok-4-1-fast-reasoning"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"

SAFE_IMMEDIATE_KEYS: frozenset[str] = frozenset({
    "delta_min_ticks", "c_vol", "c_tox", "c_kyle", "gamma", "min_edge_ticks",
    "layer_step_ticks", "layers", "reprice_ticks", "resize_frac", "flow_fv_weight",
    "trend_flow_z", "trend_vol_ratio", "event_jump_ticks", "event_cooloff_s",
    "event_sweep_mult", "event_sweep_frac", "join_best_bid",
})

FORBIDDEN_KEYS: frozenset[str] = frozenset({
    "q_max_usdc", "q_soft_frac", "base_size_usdc", "bankroll_usdc", "kelly_fraction",
    "max_open_orders_per_market", "reduce_only_hours", "halt_before_hours",
    "merge_min_size", "reward_size_mult", "use_advanced_quoting",
})

_FORBIDDEN_SUBSTRINGS = ("kill", "loss_cap", "daily_loss", "max_position", "exposure")
HIT_RATE_TRIGGER = 0.4
MIN_TRADES_FOR_HIT_RATE = 50
DEFAULT_PAPER_SECONDS = 3600
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


class MemoryLike(Protocol):
    def add(self, text: str, *, tags: list[str] | None = None) -> Any: ...
    def recent(self, n: int = 20) -> list[Any]: ...


@dataclass
class TradeJournalEntry:
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
    stripped_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_llm(cls, data: dict[str, Any]) -> ImprovementSuggestion:
        overrides = data.get("profile_overrides") or data.get("overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}
        try:
            impact_f = float(data.get("expected_impact_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            impact_f = 0.0
        impact_f = max(-50.0, min(50.0, impact_f))
        return cls(
            diagnosis=str(data.get("diagnosis", "")).strip(),
            suggestion=str(data.get("suggestion", "")).strip(),
            expected_impact_pct=impact_f,
            paper_validation_required=bool(data.get("paper_validation_required", True)),
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
    dry_run: bool = False
    draft_profile: dict[str, Any] | None = None
    live_profile: dict[str, Any] | None = None
    history_id: int | None = None
    diff: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "applied": self.applied,
            "promoted": self.promoted,
            "rejected": self.rejected,
            "paper_validated": self.paper_validated,
            "dry_run": self.dry_run,
            "diff": {k: [a, b] for k, (a, b) in self.diff.items()},
            "errors": list(self.errors),
            "history_id": self.history_id,
            "suggestion": (
                {
                    "diagnosis": self.suggestion.diagnosis,
                    "suggestion": self.suggestion.suggestion,
                    "expected_impact_pct": self.suggestion.expected_impact_pct,
                    "paper_validation_required": self.suggestion.paper_validation_required,
                    "profile_overrides": self.suggestion.profile_overrides,
                    "stripped_keys": self.suggestion.stripped_keys,
                }
                if self.suggestion else None
            ),
        }


def needs_improvement(evaluation: SelfEvaluation) -> tuple[bool, str]:
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
    rows: list[dict[str, Any]] = []
    if extras:
        for e in extras[-limit:]:
            rows.append(asdict(e))
    remaining = limit - len(rows)
    if remaining > 0:
        decisions = list(evaluation.attribution.decisions)[-remaining:]
        fill_rate = evaluation.calibration.fill_rate()
        avg_as = evaluation.calibration.avg_as()
        for d in decisions:
            rows.append({
                "pnl": float(d.get("pnl", 0.0)),
                "regime": str(d.get("regime", "")),
                "spread": 0.0,
                "fill_rate": fill_rate,
                "markout": avg_as,
                "offset": str(d.get("offset", "")),
            })
    return rows[-limit:]


def profile_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    keys = set(old) | set(new)
    return {k: (old.get(k), new.get(k)) for k in sorted(keys) if old.get(k) != new.get(k)}


def _is_forbidden_key(key: str) -> bool:
    if key in FORBIDDEN_KEYS:
        return True
    low = key.lower()
    return any(s in low for s in _FORBIDDEN_SUBSTRINGS)


def strip_forbidden(overrides: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    clean: dict[str, Any] = {}
    stripped: list[str] = []
    for k, v in overrides.items():
        if _is_forbidden_key(k):
            stripped.append(k)
        else:
            clean[k] = v
    return clean, stripped


def coerce_overrides(live: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in overrides.items():
        if k in live and live[k] is not None and v is not None:
            typ = type(live[k])
            try:
                if typ is bool:
                    out[k] = v.strip().lower() in {"1", "true", "yes", "on"} if isinstance(v, str) else bool(v)
                elif typ is int and not isinstance(v, bool):
                    out[k] = int(v)
                elif typ is float:
                    out[k] = float(v)
                else:
                    out[k] = v
            except (TypeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out


def requires_paper(overrides: dict[str, Any], llm_flag: bool) -> bool:
    if not overrides:
        return True
    if any(k not in SAFE_IMMEDIATE_KEYS for k in overrides):
        return True
    return bool(llm_flag)


def apply_overrides(profile: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    clean, _ = strip_forbidden(overrides)
    coerced = coerce_overrides(profile, clean)
    out = dict(profile)
    out.update(coerced)
    return out


def parse_llm_json(content: str | dict[str, Any] | list[Any]) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    else:
        text = str(content).strip()
    if not text:
        raise ValueError("empty LLM content")
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"LLM JSON must be an object, got {type(data).__name__}")
    return data


def call_grok_reasoning(
    *,
    system: str,
    user: str,
    api_key: str | None = None,
    model: str = REASONING_MODEL,
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    if model != REASONING_MODEL and "reasoning" not in model.lower():
        raise ValueError(f"refusing non-reasoning model {model!r}; required {REASONING_MODEL!r}")
    key = api_key if api_key is not None else os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY not set in environment / .env")

    try:
        import asyncio

        from polymaker.intelligence.agent import GrokAgent  # type: ignore
        agent = GrokAgent(api_key=key, model=model)
        async def _ago() -> dict[str, Any]:
            resp = await agent.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            return parse_llm_json(resp.content)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_ago())
    except Exception as exc:
        log.debug("GrokAgent bridge unavailable (%s); using direct HTTPS", exc)

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
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return parse_llm_json(body["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_err = RuntimeError(f"xAI HTTP {exc.code}: {detail}")
            if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise last_err from exc
        except Exception as exc:
            last_err = exc
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise
    assert last_err is not None
    raise last_err


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
            line = getattr(it, "as_prompt_line", None)
            if callable(line):
                out.append(str(line()))
            else:
                out.append(str(text if text is not None else it))
    return out


IMPROVE_SYSTEM = (
    "You are Polymaker's self-improvement analyst (reasoning required). "
    "Given a trade journal and memory, return JSON only with keys: "
    "diagnosis (str), suggestion (str), expected_impact_pct (float), "
    "paper_validation_required (bool), profile_overrides (object of "
    "StrategyProfile field → new value). "
    "Never propose changes to risk caps, daily loss, q_max_usdc, "
    "base_size_usdc, bankroll, kelly_fraction, or use_advanced_quoting. "
    "Prefer spread/regime-threshold tweaks. Be honest when no action helps."
)


class DraftStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, profile_name: str) -> Path:
        safe = re.sub(r"[^\w.\-]+", "_", profile_name) or "default"
        return self.root / f"{safe}.draft.json"

    def save(self, profile_name: str, draft: dict[str, Any], meta: dict[str, Any]) -> Path:
        path = self.path_for(profile_name)
        path.write_text(json.dumps({
            "saved_at": time.time(), "profile_name": profile_name,
            "draft": draft, "meta": meta,
        }, indent=2, default=str), encoding="utf-8")
        return path

    def load(self, profile_name: str) -> dict[str, Any] | None:
        path = self.path_for(profile_name)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def clear(self, profile_name: str) -> None:
        path = self.path_for(profile_name)
        if path.exists():
            path.unlink()


class AppliedOverridesStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, profile_name: str, overrides: dict[str, Any], *, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "updated_at": time.time(), "profile_name": profile_name,
            "overrides": overrides, "reason": reason,
        }, indent=2, default=str), encoding="utf-8")


class SelfImprover:
    def __init__(
        self, *, history: ProfileHistory, llm: Callable[..., dict[str, Any]] | None = None,
        memory: MemoryLike | None = None,
        paper_validator: Callable[[dict[str, Any]], bool] | None = None,
        paper_duration_s: float = DEFAULT_PAPER_SECONDS, profile_name: str = "default",
        draft_store: DraftStore | None = None, applied_store: AppliedOverridesStore | None = None,
    ) -> None:
        self.history = history
        self.llm = llm or call_grok_reasoning
        self.memory = memory
        self.paper_validator = paper_validator
        self.paper_duration_s = paper_duration_s
        self.profile_name = profile_name
        self.draft_store = draft_store
        self.applied_store = applied_store
        self.draft_profile: dict[str, Any] | None = None
        self.live_profile: dict[str, Any] = {}

    def set_live_profile(self, profile: dict[str, Any]) -> None:
        self.live_profile = dict(profile)

    def run(
        self, evaluation: SelfEvaluation, *, journal_extras: list[TradeJournalEntry] | None = None,
        force: bool = False, api_key: str | None = None, dry_run: bool = False,
    ) -> ImproveResult:
        triggered, reason = needs_improvement(evaluation)
        if not triggered and not force:
            return ImproveResult(triggered=False, reason=reason, live_profile=dict(self.live_profile))
        if force and not triggered:
            reason = f"forced ({reason})"

        journal = build_trade_journal(evaluation, extras=journal_extras)
        user = json.dumps({
            "trigger": reason, "summary": evaluation.summary(), "journal": journal,
            "journal_n": len(journal), "memory": _memory_snippets(self.memory),
            "current_profile": self.live_profile,
            "safe_immediate_keys": sorted(SAFE_IMMEDIATE_KEYS),
            "forbidden_keys": sorted(FORBIDDEN_KEYS),
        }, default=str)
        try:
            raw = self.llm(system=IMPROVE_SYSTEM, user=user, api_key=api_key)
            if not isinstance(raw, dict):
                raw = parse_llm_json(raw)
        except Exception as exc:
            return ImproveResult(triggered=True, reason=reason, live_profile=dict(self.live_profile),
                                 errors=[f"llm_failed: {exc}"])

        suggestion = ImprovementSuggestion.from_llm(raw)
        clean, stripped = strip_forbidden(suggestion.profile_overrides)
        clean = coerce_overrides(self.live_profile, clean)
        suggestion.profile_overrides = clean
        suggestion.stripped_keys = stripped

        if not clean:
            return ImproveResult(
                triggered=True, reason=reason, suggestion=suggestion, applied=False,
                live_profile=dict(self.live_profile),
                errors=["no_safe_overrides"] + ([f"stripped:{','.join(stripped)}"] if stripped else []),
            )

        draft = apply_overrides(self.live_profile, clean)
        diff = profile_diff(self.live_profile, draft)
        self.draft_profile = draft
        if self.draft_store is not None:
            self.draft_store.save(self.profile_name, draft, meta={
                "reason": reason, "suggestion": suggestion.suggestion,
                "diagnosis": suggestion.diagnosis,
                "diff": {k: [a, b] for k, (a, b) in diff.items()},
            })

        if dry_run:
            return ImproveResult(
                triggered=True, reason=reason, suggestion=suggestion, dry_run=True,
                draft_profile=dict(draft), live_profile=dict(self.live_profile), diff=diff,
            )

        need_paper = requires_paper(clean, suggestion.paper_validation_required)
        if need_paper:
            return self._paper_then_decide(reason=reason, suggestion=suggestion, draft=draft, diff=diff)

        hid = self.history.append(
            old_profile=self.live_profile, new_profile=draft, source="self_improve",
            reason=suggestion.suggestion or suggestion.diagnosis or "immediate",
            paper_validated=False, profile_name=self.profile_name,
        )
        self._promote_live(draft, suggestion)
        return ImproveResult(
            triggered=True, reason=reason, suggestion=suggestion, applied=True, promoted=True,
            paper_validated=False, live_profile=dict(self.live_profile), history_id=hid, diff=diff,
        )

    def _promote_live(self, draft: dict[str, Any], suggestion: ImprovementSuggestion) -> None:
        patch = profile_diff(self.live_profile, draft)
        overrides = {k: b for k, (_a, b) in patch.items()}
        self.live_profile = draft
        self.draft_profile = None
        if self.draft_store is not None:
            self.draft_store.clear(self.profile_name)
        if self.applied_store is not None and overrides:
            self.applied_store.write(self.profile_name, overrides,
                                     reason=suggestion.suggestion or suggestion.diagnosis)

    def _paper_then_decide(
        self, *, reason: str, suggestion: ImprovementSuggestion, draft: dict[str, Any],
        diff: dict[str, tuple[Any, Any]],
    ) -> ImproveResult:
        ok = False
        if self.paper_validator is not None:
            try:
                ok = bool(self.paper_validator(draft))
            except Exception as exc:
                log.warning("paper_validator raised: %s", exc)
                ok = False
        if ok:
            hid = self.history.append(
                old_profile=self.live_profile, new_profile=draft, source="self_improve",
                reason=f"[paper-ok {self.paper_duration_s:.0f}s] {suggestion.suggestion or suggestion.diagnosis}",
                paper_validated=True, profile_name=self.profile_name,
            )
            self._promote_live(draft, suggestion)
            return ImproveResult(
                triggered=True, reason=reason, suggestion=suggestion, applied=True, promoted=True,
                paper_validated=True, live_profile=dict(self.live_profile), history_id=hid, diff=diff,
            )
        hid = self.history.append(
            old_profile=self.live_profile, new_profile=self.live_profile, source="self_improve",
            reason=f"[paper-reject] {suggestion.suggestion or suggestion.diagnosis}",
            paper_validated=False, profile_name=self.profile_name,
        )
        self.draft_profile = draft
        return ImproveResult(
            triggered=True, reason=reason, suggestion=suggestion, applied=False, promoted=False,
            rejected=True, paper_validated=False, draft_profile=dict(draft),
            live_profile=dict(self.live_profile), history_id=hid, diff=diff,
            errors=["paper_validation_failed"],
        )


def evaluation_from_fill_pnls(pnls: list[tuple[float, str, str]]) -> SelfEvaluation:
    ev = SelfEvaluation()
    for pnl, regime, offset in pnls:
        ev.update(float(pnl), str(regime), str(offset))
    return ev


def run_improve_cycle(
    evaluation: SelfEvaluation, live_profile: dict[str, Any], *, db_path: str,
    force: bool = False, dry_run: bool = False, llm: Callable[..., dict[str, Any]] | None = None,
    paper_validator: Callable[[dict[str, Any]], bool] | None = None,
    memory: MemoryLike | None = None, profile_name: str = "default",
    drafts_dir: str | Path | None = None, applied_path: str | Path | None = None,
) -> ImproveResult:
    history = ProfileHistory(db_path)
    draft_store = DraftStore(drafts_dir) if drafts_dir else None
    applied = AppliedOverridesStore(applied_path) if applied_path else None
    try:
        improver = SelfImprover(
            history=history, llm=llm, memory=memory, paper_validator=paper_validator,
            profile_name=profile_name, draft_store=draft_store, applied_store=applied,
        )
        improver.set_live_profile(live_profile)
        return improver.run(evaluation, force=force, dry_run=dry_run)
    finally:
        history.close()
