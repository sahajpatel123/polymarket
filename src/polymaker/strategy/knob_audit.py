"""Audit which StrategyProfile knobs are referenced by live strategy code.

Tier-1 tooling: surfaces dead/unused profile fields so Tier-2 candidates are
not built on knobs the engine never reads. Pure filesystem scan — no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from polymaker.config import StrategyProfile


DEFAULT_ROOTS = (
    "src/polymaker/strategy",
    "src/polymaker/engine.py",
    "src/polymaker/execution/reconciler.py",
    "src/polymaker/replay",
    "src/polymaker/merge.py",
)


@dataclass(frozen=True)
class KnobAuditReport:
    used: tuple[str, ...]
    unused: tuple[str, ...]
    scanned_files: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "n_fields": len(self.used) + len(self.unused),
            "n_used": len(self.used),
            "n_unused": len(self.unused),
            "used": list(self.used),
            "unused": list(self.unused),
            "scanned_files": list(self.scanned_files),
        }


def _iter_py_files(roots: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for raw in roots:
        root = Path(raw)
        if root.is_file() and root.suffix == ".py":
            out.append(root)
        elif root.is_dir():
            out.extend(sorted(root.rglob("*.py")))
    return out


def audit_profile_knobs(
    roots: Iterable[str | Path] | None = None,
) -> KnobAuditReport:
    """Classify StrategyProfile fields as used vs unused by attribute/name refs."""
    paths = _iter_py_files(roots or DEFAULT_ROOTS)
    blob = "\n".join(p.read_text() for p in paths if p.exists())
    used: list[str] = []
    unused: list[str] = []
    for name in sorted(StrategyProfile.model_fields.keys()):
        if re.search(rf"\.{name}\b", blob) or re.search(rf"[\"']{name}[\"']", blob):
            used.append(name)
        else:
            unused.append(name)
    return KnobAuditReport(
        used=tuple(used),
        unused=tuple(unused),
        scanned_files=tuple(str(p) for p in paths if p.exists()),
    )
