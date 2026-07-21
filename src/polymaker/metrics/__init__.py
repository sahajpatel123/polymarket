"""Paper/live metrics event log — structured JSONL for Tier-1 analysis.

Separate from the raw Journal (WS replay substrate). This log is the contract
for `scripts/paper_metrics.py`: quote / cancel / fill / mark / market_meta.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TextIO


class MetricsLogger:
    """Append-only JSONL writer. Disabled when path is None."""

    def __init__(self, path: str | Path | None, *, enabled: bool = True) -> None:
        self.enabled = enabled and path is not None
        self._fh: TextIO | None = None
        if self.enabled:
            p = Path(path)  # type: ignore[arg-type]
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = p.open("a", buffering=1)

    def emit(self, event: str, **fields: Any) -> None:
        if not self.enabled or self._fh is None:
            return
        row = {"ts": fields.pop("ts", time.time()), "event": event, **fields}
        self._fh.write(json.dumps(row, default=str) + "\n")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def inventory_fields(yes_size: float, no_size: float) -> dict[str, float]:
    return {
        "inventory_yes": round(yes_size, 6),
        "inventory_no": round(no_size, 6),
        "inventory_net": round(yes_size - no_size, 6),
    }
