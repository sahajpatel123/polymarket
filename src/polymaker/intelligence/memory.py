"""Long-term agent memory — SQLite-backed, survives restarts.

Contract:
  - Table agent_memory: ts, kind, market_or_none, content, confidence
  - kind ∈ insight | finding | preference | rule
  - add / get_recent / get_for_market / search (FTS5 when available, else LIKE)
  - On boot: reload last 200 items into an in-memory ring for fast inject
  - Prune: delete rows older than 30 days with confidence < 0.3; soft cap 100k rows
  - Prompt injection: top-K by recency × relevance × confidence
  - Parse model text for lines starting with MEMORY: and persist

This is the "remember everything" feature — real persistence, not a stub.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MEMORY_KINDS = frozenset({"insight", "finding", "preference", "rule"})
BOOT_RELOAD_N = 200
MAX_ROWS = 100_000
PRUNE_AGE_S = 30 * 86400
PRUNE_CONFIDENCE = 0.3
MEMORY_LINE_RE = re.compile(
    r"^\s*MEMORY:\s*(?:\[(?P<kind>\w+)\]\s*)?(?:@(?P<market>[^\s]+)\s*)?(?P<body>.+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(slots=True)
class MemoryItem:
    id: int
    ts: float
    kind: str
    market_or_none: str | None
    content: str
    confidence: float

    def as_prompt_line(self) -> str:
        mkt = f" market={self.market_or_none}" if self.market_or_none else ""
        return f"- [{self.kind}] conf={self.confidence:.2f}{mkt}: {self.content}"


class AgentMemory:
    """SQLite long-term memory store for the LLM agent."""

    def __init__(self, db_path: str | Path = "state.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._fts = False
        self._cache: list[MemoryItem] = []
        self._init_schema()
        self.prune()
        self.reload_cache(BOOT_RELOAD_N)

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                market_or_none TEXT,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_memory_ts ON agent_memory(ts DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_memory_market ON agent_memory(market_or_none)"
        )
        self._conn.commit()
        # Try FTS5
        try:
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts
                USING fts5(content, content='agent_memory', content_rowid='id')
                """
            )
            # Keep FTS in sync via triggers (idempotent create)
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS agent_memory_ai AFTER INSERT ON agent_memory BEGIN
                  INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
                END
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS agent_memory_ad AFTER DELETE ON agent_memory BEGIN
                  INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content)
                  VALUES('delete', old.id, old.content);
                END
                """
            )
            self._conn.commit()
            self._fts = True
        except sqlite3.Error:
            self._fts = False

    def add(
        self,
        kind: str,
        content: str,
        *,
        market_or_none: str | None = None,
        confidence: float = 0.5,
        ts: float | None = None,
    ) -> MemoryItem:
        k = kind.strip().lower()
        if k not in MEMORY_KINDS:
            raise ValueError(f"kind must be one of {sorted(MEMORY_KINDS)}, got {kind!r}")
        conf = max(0.0, min(1.0, float(confidence)))
        body = content.strip()
        if not body:
            raise ValueError("content must be non-empty")
        t = float(ts if ts is not None else time.time())
        cur = self._conn.execute(
            """
            INSERT INTO agent_memory(ts, kind, market_or_none, content, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (t, k, market_or_none, body, conf),
        )
        self._conn.commit()
        item = MemoryItem(
            id=int(cur.lastrowid or 0),
            ts=t,
            kind=k,
            market_or_none=market_or_none,
            content=body,
            confidence=conf,
        )
        self._cache.insert(0, item)
        self._cache = self._cache[:BOOT_RELOAD_N]
        self._enforce_row_cap()
        return item

    def _row(self, r: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=int(r["id"]),
            ts=float(r["ts"]),
            kind=str(r["kind"]),
            market_or_none=r["market_or_none"],
            content=str(r["content"]),
            confidence=float(r["confidence"]),
        )

    def get_recent(self, n: int = 20) -> list[MemoryItem]:
        n = max(0, int(n))
        if n == 0:
            return []
        rows = self._conn.execute(
            "SELECT * FROM agent_memory ORDER BY ts DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [self._row(r) for r in rows]

    def get_for_market(self, cid: str, n: int = 20) -> list[MemoryItem]:
        n = max(0, int(n))
        rows = self._conn.execute(
            """
            SELECT * FROM agent_memory
            WHERE market_or_none = ? OR market_or_none IS NULL
            ORDER BY
              CASE WHEN market_or_none = ? THEN 0 ELSE 1 END,
              ts DESC
            LIMIT ?
            """,
            (cid, cid, n),
        ).fetchall()
        return [self._row(r) for r in rows]

    def search(self, query: str, k: int = 10) -> list[MemoryItem]:
        q = (query or "").strip()
        k = max(0, int(k))
        if not q or k == 0:
            return []
        if self._fts:
            try:
                # Simple FTS query — quote tokens
                fts_q = " ".join(t for t in re.findall(r"\w+", q) if t)
                if fts_q:
                    rows = self._conn.execute(
                        """
                        SELECT m.* FROM agent_memory m
                        JOIN agent_memory_fts f ON f.rowid = m.id
                        WHERE agent_memory_fts MATCH ?
                        ORDER BY m.ts DESC
                        LIMIT ?
                        """,
                        (fts_q, k),
                    ).fetchall()
                    if rows:
                        return [self._row(r) for r in rows]
            except sqlite3.Error:
                pass
        # LIKE fallback
        like = f"%{q}%"
        rows = self._conn.execute(
            """
            SELECT * FROM agent_memory
            WHERE content LIKE ? COLLATE NOCASE
            ORDER BY ts DESC
            LIMIT ?
            """,
            (like, k),
        ).fetchall()
        return [self._row(r) for r in rows]

    def reload_cache(self, n: int = BOOT_RELOAD_N) -> list[MemoryItem]:
        self._cache = self.get_recent(n)
        return list(self._cache)

    def prune(self) -> int:
        """Remove old low-confidence rows; trim if over MAX_ROWS. Returns deleted count."""
        cutoff = time.time() - PRUNE_AGE_S
        cur = self._conn.execute(
            """
            DELETE FROM agent_memory
            WHERE ts < ? AND confidence < ?
            """,
            (cutoff, PRUNE_CONFIDENCE),
        )
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self._conn.commit()
        deleted += self._enforce_row_cap()
        return deleted

    def _enforce_row_cap(self) -> int:
        n = self._conn.execute("SELECT COUNT(*) AS c FROM agent_memory").fetchone()
        count = int(n["c"] if n else 0)
        if count <= MAX_ROWS:
            return 0
        excess = count - MAX_ROWS
        self._conn.execute(
            """
            DELETE FROM agent_memory WHERE id IN (
              SELECT id FROM agent_memory ORDER BY ts ASC LIMIT ?
            )
            """,
            (excess,),
        )
        self._conn.commit()
        return excess

    def score_for_prompt(
        self,
        item: MemoryItem,
        *,
        query: str = "",
        market: str | None = None,
        now: float | None = None,
    ) -> float:
        """recency × relevance × confidence."""
        now = now if now is not None else time.time()
        age_h = max(0.0, (now - item.ts) / 3600.0)
        recency = 1.0 / (1.0 + age_h / 24.0)  # half-ish life ~day
        conf = max(0.0, min(1.0, item.confidence))
        rel = 0.3
        q = query.lower()
        body = item.content.lower()
        if q:
            tokens = [t for t in re.findall(r"\w+", q) if len(t) > 2]
            if tokens:
                hits = sum(1 for t in tokens if t in body)
                rel = 0.2 + 0.8 * (hits / len(tokens))
        if market and item.market_or_none == market:
            rel = min(1.0, rel + 0.3)
        elif market and item.market_or_none is None:
            rel = min(1.0, rel + 0.05)
        return recency * rel * conf

    def inject_for_prompt(
        self,
        *,
        query: str = "",
        market: str | None = None,
        k: int = 8,
        now: float | None = None,
    ) -> str:
        """Return a prompt block with top-K memory items for injection."""
        pool = self.get_recent(200)
        if market:
            # Prefer market-specific + search hits
            pool = list({i.id: i for i in (self.get_for_market(market, 50) + pool)}.values())
        if query:
            found = self.search(query, k=30)
            pool = list({i.id: i for i in (found + pool)}.values())
        ranked = sorted(
            pool,
            key=lambda it: self.score_for_prompt(it, query=query, market=market, now=now),
            reverse=True,
        )[: max(0, k)]
        if not ranked:
            return "(no long-term memory yet)"
        lines = ["## Long-term memory (ranked)", *[it.as_prompt_line() for it in ranked]]
        return "\n".join(lines)

    def parse_and_store_from_text(
        self,
        text: str,
        *,
        default_market: str | None = None,
        default_confidence: float = 0.6,
    ) -> list[MemoryItem]:
        """Parse MEMORY: lines and persist. Returns created items."""
        created: list[MemoryItem] = []
        for m in MEMORY_LINE_RE.finditer(text or ""):
            kind = (m.group("kind") or "insight").lower()
            if kind not in MEMORY_KINDS:
                kind = "insight"
            market = m.group("market") or default_market
            body = (m.group("body") or "").strip()
            if not body:
                continue
            created.append(
                self.add(
                    kind,
                    body,
                    market_or_none=market,
                    confidence=default_confidence,
                )
            )
        return created


def build_messages_with_memory(
    memory: AgentMemory,
    *,
    system: str,
    user: str,
    query: str = "",
    market: str | None = None,
    k: int = 8,
) -> list[dict[str, str]]:
    """Inject top-K memory into the system prompt before an LLM call."""
    block = memory.inject_for_prompt(query=query or user[:200], market=market, k=k)
    sys = f"{system}\n\n{block}"
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user},
    ]
