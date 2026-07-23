"""Paper log discovery helpers shared by Tier-1 evidence scripts.

Prefer the richest existing paper/metrics JSONL so a tiny accidental
`logs/paper.jsonl` cannot shadow a long-running `livecfg/logs` collector.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path


def _ts(obj: dict) -> float | None:
    for key in ("ts", "timestamp", "time"):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                try:
                    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
    return None


def paper_log_score(path: Path, *, sample_limit: int = 5000) -> tuple[float, int]:
    """Return (runtime_hours, n_json_lines) for ranking paper logs.

    Runtime prefers the requote timeline so outage noise
    (``market_ws_dropped`` / ``get_full_book_failed``) cannot make a stale
    collector look "richer" than an active one (aligned with T1-52 gate).
    """
    if not path.exists():
        return (-1.0, -1)
    times_all: list[float] = []
    times_requote: list[float] = []
    n_json = 0
    try:
        with path.open() as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                n_json += 1
                event = str(obj.get("event") or obj.get("msg") or "")
                t = _ts(obj)
                if t is not None:
                    times_all.append(t)
                    if event == "requote" or "requote" in event:
                        times_requote.append(t)
                # After sample_limit lines, still count remaining cheaply for size
                if i + 1 >= sample_limit:
                    for rest in fh:
                        if rest.strip():
                            n_json += 1
                    break
    except OSError:
        return (-1.0, -1)

    def _span_h(times: list[float]) -> float:
        if len(times) >= 2:
            return max(0.0, (max(times) - min(times)) / 3600.0)
        return 0.0

    runtime_h = _span_h(times_requote)
    if runtime_h <= 0.0:
        runtime_h = _span_h(times_all)
    return (runtime_h, n_json)


def pick_richest_log(candidates: Iterable[Path]) -> Path | None:
    """Pick existing path with highest (runtime_hours, n_json_lines)."""
    best: Path | None = None
    best_score = (-1.0, -1)
    for raw in candidates:
        path = Path(raw)
        if not path.exists():
            continue
        score = paper_log_score(path)
        if score > best_score:
            best_score = score
            best = path
    return best


def default_paper_candidates() -> tuple[Path, ...]:
    """Active paper.jsonl paths plus dated rotations (T1-96).

    Overnight log rotation writes ``paper.jsonl.YYYY-MM-DD`` while a new
    empty ``paper.jsonl`` continues. Discovery must consider the archive or
    the gate collapses to outage-noise-only runtime.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    for base in (Path("livecfg/logs"), Path("logs")):
        _add(base / "paper.jsonl")
        if base.is_dir():
            for rotated in sorted(base.glob("paper.jsonl.*")):
                _add(rotated)
    return tuple(found)


class _DefaultPaperCandidates:
    """Lazy iterable/indexable view of :func:`default_paper_candidates`."""

    def __iter__(self):
        return iter(default_paper_candidates())

    def __getitem__(self, index: int) -> Path:
        return default_paper_candidates()[index]

    def __len__(self) -> int:
        return len(default_paper_candidates())


DEFAULT_PAPER_CANDIDATES = _DefaultPaperCandidates()

DEFAULT_METRICS_CANDIDATES = (
    Path("livecfg/logs/metrics-paper.jsonl"),
    Path("logs/metrics-paper.jsonl"),
)
