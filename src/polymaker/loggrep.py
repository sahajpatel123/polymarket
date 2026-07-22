"""Filter JSON log lines by market ID and time range (T1-04)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_ts(value: str | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def row_ts(obj: dict[str, Any]) -> float | None:
    for key in ("timestamp", "ts", "time"):
        if key in obj:
            return parse_ts(obj[key])
    return None


def row_market(obj: dict[str, Any]) -> str:
    for key in ("condition_id", "cid", "market", "market_id"):
        v = obj.get(key)
        if v is not None:
            return str(v)
    return ""


def iter_log_paths(path: Path) -> list[Path]:
    if not path.exists():
        siblings = sorted(path.parent.glob(path.name + ".*")) if path.parent.exists() else []
        return [p for p in siblings if p.is_file()]
    paths = [path]
    for p in sorted(path.parent.glob(path.name + ".*")):
        if p.is_file():
            paths.append(p)
    return paths


def market_matches(row_market_id: str, needle: str) -> bool:
    if not needle:
        return True
    if needle in row_market_id:
        return True
    return len(needle) >= 8 and needle[:8] in row_market_id


def grep_logs(
    path: Path,
    *,
    condition_id: str | None = None,
    since: float | None = None,
    until: float | None = None,
    event: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in iter_log_paths(path):
        with p.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if condition_id and not market_matches(row_market(obj), condition_id):
                    continue
                ts = row_ts(obj)
                if since is not None and (ts is None or ts < since):
                    continue
                if until is not None and (ts is None or ts > until):
                    continue
                if event is not None and str(obj.get("event") or "") != event:
                    continue
                out.append(obj)
    out.sort(key=lambda r: row_ts(r) or 0.0)
    return out
