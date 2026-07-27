"""End-of-day review: day summary → Grok reasoning → markdown report + memory."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

from polymaker.intelligence.self_improve import (
    REASONING_MODEL,
    call_grok_reasoning,
    parse_llm_json,
)

log = logging.getLogger("polymaker.intelligence.review")
VALID_GRADES = frozenset("ABCDEF")
MEMORY_LINE_RE = re.compile(
    r"^\s*MEMORY:\s*(?:\[(?P<kind>\w+)\]\s*)?(?:@(?P<market>[^\s]+)\s*)?(?P<body>.+)\s*$",
    re.IGNORECASE | re.MULTILINE,
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
    kind: str = "insight"
    confidence: float = 0.5

    @property
    def content(self) -> str:
        return self.text


class LocalMemoryStore:
    """Fallback memory with AgentMemory-compatible columns."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL DEFAULT 'insight',
                market_or_none TEXT,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                tags TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.commit()
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(agent_memory)").fetchall()}
        self._legacy = "text" in cols and "content" not in cols

    def close(self) -> None:
        self._conn.close()

    def add(self, text: str, *, tags: list[str] | None = None) -> int:
        tag_s = ",".join(tags or [])
        kind = "insight"
        if tags:
            for t in tags:
                if t in {"insight", "finding", "preference", "rule"}:
                    kind = t
                    break
        if self._legacy:
            cur = self._conn.execute(
                "INSERT INTO agent_memory (ts, text, tags) VALUES (?, ?, ?)",
                (time.time(), text, tag_s),
            )
        else:
            cur = self._conn.execute(
                "INSERT INTO agent_memory (ts, kind, market_or_none, content, confidence, tags) "
                "VALUES (?, ?, NULL, ?, 0.6, ?)",
                (time.time(), kind, text, tag_s),
            )
        self._conn.commit()
        return int(cur.lastrowid)

    def _rows_to_items(self, rows: list[sqlite3.Row]) -> list[MemoryItem]:
        out: list[MemoryItem] = []
        for r in rows:
            keys = r.keys()
            text = str(r["content"] if "content" in keys else r["text"])
            tags = str(r["tags"]) if "tags" in keys else ""
            kind = str(r["kind"]) if "kind" in keys else "insight"
            conf = float(r["confidence"]) if "confidence" in keys else 0.5
            out.append(MemoryItem(id=int(r["id"]), ts=float(r["ts"]), text=text,
                                  tags=tags, kind=kind, confidence=conf))
        return out

    def recent(self, n: int = 20) -> list[MemoryItem]:
        rows = self._conn.execute(
            "SELECT * FROM agent_memory ORDER BY ts DESC, id DESC LIMIT ?",
            (max(0, int(n)),),
        ).fetchall()
        return self._rows_to_items(rows)

    def search(self, query: str, *, limit: int = 20) -> list[MemoryItem]:
        q = f"%{query}%"
        col = "text" if self._legacy else "content"
        rows = self._conn.execute(
            f"SELECT * FROM agent_memory WHERE {col} LIKE ? OR tags LIKE ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (q, q, max(0, int(limit))),
        ).fetchall()
        return self._rows_to_items(rows)


class _AgentMemoryAdapter:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def add(self, text: str, *, tags: list[str] | None = None) -> Any:
        kind = "insight"
        if tags:
            for t in tags:
                if t in {"insight", "finding", "preference", "rule"}:
                    kind = t
                    break
        try:
            return self._inner.add(text, kind=kind, confidence=0.6)
        except TypeError:
            return self._inner.add(text)

    def recent(self, n: int = 20) -> list[Any]:
        if hasattr(self._inner, "get_recent"):
            return list(self._inner.get_recent(n))
        return list(self._inner.recent(n))

    def search(self, query: str, *, limit: int = 20) -> list[Any]:
        return list(self._inner.search(query, limit=limit))


def load_memory(db_path: str | Path) -> MemoryLike:
    try:
        from polymaker.intelligence.memory import AgentMemory  # type: ignore
        return _AgentMemoryAdapter(AgentMemory(db_path))
    except Exception:
        try:
            from polymaker.intelligence.memory import MemoryStore  # type: ignore
            return MemoryStore(db_path)  # type: ignore[return-value]
        except Exception:
            return LocalMemoryStore(db_path)


@dataclass
class DaySummary:
    date_utc: str
    pnl: float = 0.0
    drawdown: float = 0.0
    fills: int = 0
    markouts: list[float] = field(default_factory=list)
    regime_history: list[str] = field(default_factory=list)
    self_improve_actions: list[str] = field(default_factory=list)
    memory_growth: int = 0
    hit_rate: float | None = None
    equity: float | None = None
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
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_llm(cls, data: dict[str, Any]) -> ReviewResult:
        problems = data.get("top_3_problems") or []
        wins = data.get("top_3_wins") or []
        mem = list(data.get("new_memory_items") or [])
        for key in ("narrative", "notes", "commentary"):
            blob = data.get(key)
            if isinstance(blob, str):
                for m in MEMORY_LINE_RE.finditer(blob):
                    mem.append(m.group("body").strip())
        adj = data.get("tomorrow_baseline_adjustments") or {}
        if not isinstance(adj, dict):
            adj = {"note": adj}
        grade = str(data.get("grade", "C")).strip().upper()[:1]
        if grade not in VALID_GRADES:
            grade = "C"
        return cls(
            grade=grade,
            top_3_problems=[str(x) for x in list(problems)[:3]],
            top_3_wins=[str(x) for x in list(wins)[:3]],
            tomorrow_baseline_adjustments=dict(adj),
            new_memory_items=[str(x) for x in mem if str(x).strip()],
            raw=dict(data),
        )


REVIEW_SYSTEM = (
    "You are Polymaker's end-of-day reviewer using careful reasoning "
    f"(model contract: {REASONING_MODEL}). "
    "Return JSON only with keys: grade (A-F), top_3_problems (array of ≤3 "
    "strings), top_3_wins (array of ≤3 strings), "
    "tomorrow_baseline_adjustments (object of profile field tweaks — never "
    "risk caps / daily-loss / max position), "
    "new_memory_items (array of short insight strings). "
    "Be honest about edge; never invent fills."
)


def default_reviews_dir(config_dir: str | Path = "livecfg") -> Path:
    return Path(config_dir) / "daily_reviews"


def utc_review_cron_hour_minute() -> tuple[int, int]:
    return 23, 55


def should_run_eod_review(now: datetime | None = None, *, window_minutes: int = 5) -> bool:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    h, m = utc_review_cron_hour_minute()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < target:
        return False
    return (now - target) <= timedelta(minutes=window_minutes)


def gather_day_summary(
    db_path: str | Path, *, date_utc: str | None = None,
    memory: MemoryLike | None = None, self_improve_actions: list[str] | None = None,
) -> DaySummary:
    day = date_utc or datetime.now(UTC).strftime("%Y-%m-%d")
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    end = start + timedelta(days=1)
    t0, t1 = start.timestamp(), end.timestamp()
    pnl, drawdown, fills = 0.0, 0.0, 0
    equity: float | None = None
    path = Path(db_path)
    if path.exists():
        try:
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT equity, daily_pnl FROM pnl_snapshots WHERE ts >= ? AND ts < ? "
                "ORDER BY ts DESC LIMIT 1", (t0, t1),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT equity, daily_pnl FROM pnl_snapshots ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            if row is not None:
                pnl = float(row["daily_pnl"] or 0.0)
                equity = float(row["equity"]) if row["equity"] is not None else None
            eqs = [
                float(r["equity"])
                for r in conn.execute(
                    "SELECT equity FROM pnl_snapshots WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                    (t0, t1),
                ).fetchall()
                if r["equity"] is not None
            ]
            if eqs:
                peak = eqs[0]
                dd = 0.0
                for e in eqs:
                    peak = max(peak, e)
                    dd = max(dd, peak - e)
                drawdown = dd
            try:
                fills = int(conn.execute(
                    "SELECT COUNT(*) n FROM fills WHERE ts >= ? AND ts < ?", (t0, t1)
                ).fetchone()["n"])
            except Exception:
                try:
                    fills = int(conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"])
                except Exception:
                    fills = 0
            conn.close()
        except Exception as exc:
            log.debug("gather_day_summary db error: %s", exc)
    mem_n = 0
    if memory is not None:
        try:
            mem_n = len(memory.recent(500))
        except Exception:
            mem_n = 0
    return DaySummary(
        date_utc=day, pnl=pnl, drawdown=drawdown, fills=fills,
        self_improve_actions=list(self_improve_actions or []),
        memory_growth=mem_n, equity=equity,
    )


def render_markdown(summary: DaySummary, result: ReviewResult) -> str:
    lines = [
        f"# Polymaker Daily Review — {summary.date_utc}", "",
        f"**Grade:** {result.grade}", "", "## Day snapshot", "",
        f"- PnL: `{summary.pnl:+.4f}` USDC",
        f"- Equity: `{summary.equity}`",
        f"- Drawdown: `{summary.drawdown:.4f}`",
        f"- Fills: `{summary.fills}`",
        f"- Hit rate: `{summary.hit_rate}`",
        f"- Memory items (recent window): `{summary.memory_growth}`",
        f"- Self-improve actions: {len(summary.self_improve_actions)}",
        "", "### Regime history", "",
    ]
    if summary.regime_history:
        for r in summary.regime_history[-40:]:
            lines.append(f"- {r}")
    else:
        lines.append("- _(none)_")
    lines += ["", "### Markouts (sample)", ""]
    if summary.markouts:
        lines.append("`" + ", ".join(f"{m:.4f}" for m in summary.markouts[-20:]) + "`")
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
    lines += ["", "## Self-improvement actions today", ""]
    for a in summary.self_improve_actions:
        lines.append(f"- {a}")
    if not summary.self_improve_actions:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines)


def persist_review(summary: DaySummary, result: ReviewResult, *, reviews_dir: Path) -> Path:
    reviews_dir.mkdir(parents=True, exist_ok=True)
    path = reviews_dir / f"{summary.date_utc}.md"
    text = render_markdown(summary, result)
    path.write_text(text, encoding="utf-8")
    (reviews_dir / "LATEST.md").write_text(text, encoding="utf-8")
    result.report_path = path
    return path


def push_memory_items(memory: MemoryLike | None, items: list[str]) -> int:
    if memory is None or not items:
        return 0
    n = 0
    seen: set[str] = set()
    for text in items:
        t = text.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        try:
            memory.add(t, tags=["daily_review", "insight"])
            n += 1
        except Exception as exc:
            log.debug("memory.add failed: %s", exc)
    return n


def run_daily_review(
    summary: DaySummary | None = None, *, llm: Callable[..., dict[str, Any]] | None = None,
    memory: MemoryLike | None = None, reviews_dir: str | Path | None = None,
    api_key: str | None = None, date_utc: str | None = None, dry_run: bool = False,
) -> ReviewResult:
    day = date_utc or datetime.now(UTC).strftime("%Y-%m-%d")
    if summary is None:
        summary = DaySummary(date_utc=day)
    elif not summary.date_utc:
        summary.date_utc = day
    call = llm or call_grok_reasoning
    user = json.dumps(asdict(summary), default=str)
    try:
        raw = call(system=REVIEW_SYSTEM, user=user, api_key=api_key)
        if not isinstance(raw, dict):
            raw = parse_llm_json(raw)
    except Exception as exc:
        result = ReviewResult(
            grade="F", top_3_problems=[f"review LLM failed: {exc}"], top_3_wins=[],
            tomorrow_baseline_adjustments={}, new_memory_items=[], errors=[str(exc)],
        )
        if not dry_run:
            persist_review(summary, result, reviews_dir=Path(reviews_dir) if reviews_dir else default_reviews_dir())
        return result
    result = ReviewResult.from_llm(raw)
    result.dry_run = dry_run
    if dry_run:
        return result
    out_dir = Path(reviews_dir) if reviews_dir else default_reviews_dir()
    persist_review(summary, result, reviews_dir=out_dir)
    push_memory_items(memory, result.new_memory_items)
    return result
