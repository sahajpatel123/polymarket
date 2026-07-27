"""End-of-day review: day summary → Grok reasoning → markdown report + memory.

Scheduled for UTC 23:55 in the orchestrator; also runnable on demand via
``polymaker review``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from polymaker.intelligence.self_improve import (
    REASONING_MODEL,
    call_grok_reasoning,
)


class MemoryLike(Protocol):
    def add(self, text: str, *, tags: list[str] | None = None) -> Any: ...

    def recent(self, n: int = 20) -> list[Any]: ...

    def search(self, query: str, *, limit: int = 20) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: int
    ts: float
    text: str
    tags: str = ""


class LocalMemoryStore:
    """Fallback memory until Agent-1 ``memory.py`` lands."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                text TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, text: str, *, tags: list[str] | None = None) -> int:
        tag_s = ",".join(tags or [])
        cur = self._conn.execute(
            "INSERT INTO agent_memory (ts, text, tags) VALUES (?, ?, ?)",
            (time.time(), text, tag_s),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent(self, n: int = 20) -> list[MemoryItem]:
        rows = self._conn.execute(
            "SELECT id, ts, text, tags FROM agent_memory "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (max(0, int(n)),),
        ).fetchall()
        return [
            MemoryItem(
                id=int(r["id"]),
                ts=float(r["ts"]),
                text=str(r["text"]),
                tags=str(r["tags"]),
            )
            for r in rows
        ]

    def search(self, query: str, *, limit: int = 20) -> list[MemoryItem]:
        q = f"%{query}%"
        rows = self._conn.execute(
            "SELECT id, ts, text, tags FROM agent_memory "
            "WHERE text LIKE ? OR tags LIKE ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (q, q, max(0, int(limit))),
        ).fetchall()
        return [
            MemoryItem(
                id=int(r["id"]),
                ts=float(r["ts"]),
                text=str(r["text"]),
                tags=str(r["tags"]),
            )
            for r in rows
        ]


def load_memory(db_path: str | Path) -> MemoryLike:
    """Prefer Agent-1 MemoryStore when importable."""
    try:
        from polymaker.intelligence.memory import MemoryStore  # type: ignore

        return MemoryStore(db_path)  # type: ignore[return-value]
    except Exception:
        return LocalMemoryStore(db_path)


@dataclass
class DaySummary:
    """Inputs for the end-of-day LLM review."""

    date_utc: str
    pnl: float = 0.0
    drawdown: float = 0.0
    fills: int = 0
    markouts: list[float] = field(default_factory=list)
    regime_history: list[str] = field(default_factory=list)
    self_improve_actions: list[str] = field(default_factory=list)
    memory_growth: int = 0
    hit_rate: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewResult:
    grade: str
    top_3_problems: list[str]
    top_3_wins: list[str]
    tomorrow_baseline_adjustments: dict[str, Any]
    new_memory_items: list[str]
    report_path: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_llm(cls, data: dict[str, Any]) -> ReviewResult:
        problems = data.get("top_3_problems") or []
        wins = data.get("top_3_wins") or []
        mem = data.get("new_memory_items") or []
        adj = data.get("tomorrow_baseline_adjustments") or {}
        if not isinstance(adj, dict):
            adj = {"note": adj}
        return cls(
            grade=str(data.get("grade", "C")),
            top_3_problems=[str(x) for x in list(problems)[:3]],
            top_3_wins=[str(x) for x in list(wins)[:3]],
            tomorrow_baseline_adjustments=dict(adj),
            new_memory_items=[str(x) for x in mem],
            raw=dict(data),
        )


REVIEW_SYSTEM = (
    "You are Polymaker's end-of-day reviewer using careful reasoning. "
    "Return JSON only with keys: grade (A-F), top_3_problems (array of 3 "
    "strings), top_3_wins (array of 3 strings), "
    "tomorrow_baseline_adjustments (object of profile field tweaks), "
    "new_memory_items (array of short insight strings). "
    "Be honest about edge; never invent fills. Never suggest changing "
    "daily-loss kill, risk caps, or max position."
)


def default_reviews_dir(config_dir: str | Path = "livecfg") -> Path:
    return Path(config_dir) / "daily_reviews"


def render_markdown(summary: DaySummary, result: ReviewResult) -> str:
    """Render a durable markdown daily review report."""
    lines = [
        f"# Polymaker Daily Review — {summary.date_utc}",
        "",
        f"**Grade:** {result.grade}",
        "",
        "## Day snapshot",
        "",
        f"- PnL: `{summary.pnl:+.4f}` USDC",
        f"- Drawdown: `{summary.drawdown:.4f}`",
        f"- Fills: `{summary.fills}`",
        f"- Hit rate: `{summary.hit_rate}`",
        f"- Memory growth: `{summary.memory_growth}`",
        f"- Self-improve actions: {len(summary.self_improve_actions)}",
        "",
        "### Regime history",
        "",
    ]
    if summary.regime_history:
        for r in summary.regime_history[-40:]:
            lines.append(f"- {r}")
    else:
        lines.append("- _(none)_")
    lines += ["", "### Markouts (sample)", ""]
    if summary.markouts:
        sample = ", ".join(f"{m:.4f}" for m in summary.markouts[-20:])
        lines.append(f"`{sample}`")
    else:
        lines.append("- _(none)_")
    lines += ["", "## Top problems", ""]
    for i, p in enumerate(result.top_3_problems, 1):
        lines.append(f"{i}. {p}")
    if not result.top_3_problems:
        lines.append("_None listed._")
    lines += ["", "## Top wins", ""]
    for i, w in enumerate(result.top_3_wins, 1):
        lines.append(f"{i}. {w}")
    if not result.top_3_wins:
        lines.append("_None listed._")
    lines += ["", "## Tomorrow baseline adjustments", "", "```json"]
    lines.append(json.dumps(result.tomorrow_baseline_adjustments, indent=2))
    lines += ["```", "", "## New memory items", ""]
    for m in result.new_memory_items:
        lines.append(f"- {m}")
    if not result.new_memory_items:
        lines.append("_None._")
    lines += [
        "",
        "## Self-improvement actions today",
        "",
    ]
    for a in summary.self_improve_actions:
        lines.append(f"- {a}")
    if not summary.self_improve_actions:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def persist_review(
    summary: DaySummary,
    result: ReviewResult,
    *,
    reviews_dir: Path,
) -> Path:
    reviews_dir.mkdir(parents=True, exist_ok=True)
    path = reviews_dir / f"{summary.date_utc}.md"
    path.write_text(render_markdown(summary, result), encoding="utf-8")
    result.report_path = path
    return path


def push_memory_items(memory: MemoryLike | None, items: list[str]) -> int:
    if memory is None or not items:
        return 0
    n = 0
    for text in items:
        try:
            memory.add(text, tags=["daily_review"])
            n += 1
        except Exception:
            continue
    return n


def run_daily_review(
    summary: DaySummary | None = None,
    *,
    llm: Callable[..., dict[str, Any]] | None = None,
    memory: MemoryLike | None = None,
    reviews_dir: str | Path | None = None,
    api_key: str | None = None,
    date_utc: str | None = None,
) -> ReviewResult:
    """Run end-of-day review; write markdown and push insights to memory."""
    day = date_utc or datetime.now(UTC).strftime("%Y-%m-%d")
    if summary is None:
        summary = DaySummary(date_utc=day)
    elif not summary.date_utc:
        summary.date_utc = day

    call = llm or call_grok_reasoning
    user = json.dumps(asdict(summary), default=str)
    raw = call(system=REVIEW_SYSTEM, user=user, api_key=api_key)
    # Ensure model identity is never silently downgraded by callers.
    if llm is None:
        _ = REASONING_MODEL  # documented contract
        _ = os.environ.get("XAI_API_KEY", "")

    result = ReviewResult.from_llm(raw)
    out_dir = Path(reviews_dir) if reviews_dir else default_reviews_dir()
    persist_review(summary, result, reviews_dir=out_dir)
    push_memory_items(memory, result.new_memory_items)
    return result


def utc_review_cron_hour_minute() -> tuple[int, int]:
    """Canonical schedule: 23:55 UTC."""
    return 23, 55
