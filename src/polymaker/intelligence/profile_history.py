"""Append-only profile change history with rollback support.

Every self-improvement / manual / hot-reload profile mutation is logged to
SQLite so operators can inspect diffs and roll back to a prior snapshot.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Source = Literal["self_improve", "manual", "hot_reload", "review", "rollback"]


@dataclass(frozen=True, slots=True)
class ProfileChange:
    """One row from the profile_history table."""

    id: int
    ts: float
    old_profile_json: str
    new_profile_json: str
    source: str
    reason: str
    paper_validated: bool
    profile_name: str = "default"

    @property
    def old_profile(self) -> dict[str, Any]:
        return json.loads(self.old_profile_json)

    @property
    def new_profile(self) -> dict[str, Any]:
        return json.loads(self.new_profile_json)

    def diff(self) -> dict[str, tuple[Any, Any]]:
        """Return {key: (old, new)} for keys that changed."""
        old, new = self.old_profile, self.new_profile
        keys = set(old) | set(new)
        return {
            k: (old.get(k), new.get(k))
            for k in sorted(keys)
            if old.get(k) != new.get(k)
        }

    def human_diff(self) -> str:
        """One-line-per-key human readable diff."""
        parts = [f"{k}: {a!r} → {b!r}" for k, (a, b) in self.diff().items()]
        return "; ".join(parts) if parts else "(no changes)"


class ProfileHistory:
    """SQLite-backed append-only log of strategy profile mutations."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                old_profile_json TEXT NOT NULL,
                new_profile_json TEXT NOT NULL,
                source TEXT NOT NULL,
                reason TEXT NOT NULL,
                paper_validated INTEGER NOT NULL DEFAULT 0,
                profile_name TEXT NOT NULL DEFAULT 'default'
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profile_history_ts ON profile_history(ts)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profile_history_name "
            "ON profile_history(profile_name)"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) n FROM profile_history").fetchone()
        return int(row["n"]) if row else 0

    def append(
        self,
        *,
        old_profile: dict[str, Any],
        new_profile: dict[str, Any],
        source: Source,
        reason: str,
        paper_validated: bool = False,
        profile_name: str = "default",
        ts: float | None = None,
    ) -> int:
        """Append a profile change. Returns row id."""
        when = float(time.time() if ts is None else ts)
        cur = self._conn.execute(
            """
            INSERT INTO profile_history
              (ts, old_profile_json, new_profile_json, source, reason,
               paper_validated, profile_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                when,
                json.dumps(old_profile, sort_keys=True, default=str),
                json.dumps(new_profile, sort_keys=True, default=str),
                source,
                reason[:2000],
                1 if paper_validated else 0,
                profile_name,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_recent(
        self, n: int = 20, *, profile_name: str | None = None
    ) -> list[ProfileChange]:
        """Return the n most recent changes (newest first)."""
        if profile_name:
            rows = self._conn.execute(
                """
                SELECT id, ts, old_profile_json, new_profile_json, source, reason,
                       paper_validated, profile_name
                FROM profile_history
                WHERE profile_name = ?
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (profile_name, max(0, int(n))),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT id, ts, old_profile_json, new_profile_json, source, reason,
                       paper_validated, profile_name
                FROM profile_history
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (max(0, int(n)),),
            ).fetchall()
        return [self._row(r) for r in rows]

    def get_by_id(self, row_id: int) -> ProfileChange | None:
        row = self._conn.execute(
            """
            SELECT id, ts, old_profile_json, new_profile_json, source, reason,
                   paper_validated, profile_name
            FROM profile_history WHERE id = ?
            """,
            (int(row_id),),
        ).fetchone()
        return self._row(row) if row is not None else None

    def get_at(self, ts: float) -> ProfileChange | None:
        """Return the latest change at or before ``ts``, or None."""
        row = self._conn.execute(
            """
            SELECT id, ts, old_profile_json, new_profile_json, source, reason,
                   paper_validated, profile_name
            FROM profile_history
            WHERE ts <= ?
            ORDER BY ts DESC, id DESC
            LIMIT 1
            """,
            (float(ts),),
        ).fetchone()
        return self._row(row) if row is not None else None

    def latest(self, *, profile_name: str | None = None) -> ProfileChange | None:
        rows = self.list_recent(1, profile_name=profile_name)
        return rows[0] if rows else None

    def rollback(
        self,
        to_ts: float,
        *,
        reason: str = "rollback",
        profile_name: str | None = None,
    ) -> dict[str, Any]:
        """Restore the profile that was live just before the change at ``to_ts``."""
        target = self.get_at(to_ts)
        if target is None:
            raise ValueError(f"no profile_history entry at or before ts={to_ts}")

        restored = target.old_profile
        name = profile_name or target.profile_name
        current = self.latest(profile_name=name)
        current_profile = current.new_profile if current is not None else {}
        self.append(
            old_profile=current_profile,
            new_profile=restored,
            source="rollback",
            reason=f"{reason} (to_ts={to_ts})",
            paper_validated=True,
            profile_name=name,
        )
        return restored

    def self_improve_actions_today(self, day_start_ts: float) -> list[str]:
        """Human-readable self_improve reasons since ``day_start_ts``."""
        rows = self._conn.execute(
            """
            SELECT reason, paper_validated FROM profile_history
            WHERE source = 'self_improve' AND ts >= ?
            ORDER BY ts ASC
            """,
            (float(day_start_ts),),
        ).fetchall()
        out: list[str] = []
        for r in rows:
            flag = "paper-ok" if r["paper_validated"] else "applied/reject"
            out.append(f"{flag}: {r['reason']}")
        return out

    @staticmethod
    def _row(r: sqlite3.Row) -> ProfileChange:
        return ProfileChange(
            id=int(r["id"]),
            ts=float(r["ts"]),
            old_profile_json=str(r["old_profile_json"]),
            new_profile_json=str(r["new_profile_json"]),
            source=str(r["source"]),
            reason=str(r["reason"]),
            paper_validated=bool(r["paper_validated"]),
            profile_name=str(r["profile_name"]),
        )
