"""Execution intelligence: order timing, anti-gaming, iceberg sizing.

A naive market maker places orders immediately when a signal is
generated. An intelligent one considers:
- When to place: time of day, latency to exchange
- Anti-gaming: avoid patterns that informed traders exploit
- Iceberg sizing: hide true order size to avoid detection

Components:

1. Order timing optimizer:
   - Delay quote placement by random jitter to avoid pattern detection
   - Avoid placing at round-number times
   - Batch multiple quote updates to reduce API calls

2. Anti-gaming detector:
   - Detect when the same counterparty repeatedly picks us off
   - Detect when our quotes are systematically sniped after news
   - Adjust spread when gaming is detected

3. Iceberg sizing:
   - Split large orders into smaller chunks
   - Hide true order size from the order book
   - Maintain consistent displayed size

4. Smart execution:
   - Place market-clearing orders at top of band
   - Use passive orders to earn maker rebates
   - Avoid crossing the spread (taker fees)

Pure state machines. The engine queries for execution parameters
before each order placement.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderTimingOptimizer:
    """Decide WHEN to place orders with smart timing.

    Adds random jitter to avoid pattern detection, batches updates
    to reduce API calls, and avoids predictable timing patterns.
    """

    min_jitter_s: float = 0.1
    max_jitter_s: float = 2.0
    last_quote_ts: float | None = None  # None = never quoted (0.0 is a valid ts)
    pending_quote: bool = False
    pending_quote_reason: str = ""
    batch_window_s: float = 0.5
    quote_buffer: deque = field(default_factory=lambda: deque(maxlen=20))

    def should_quote_now(self, ts: float) -> bool:
        """True if enough time has passed since the last quote.

        Adds random jitter to avoid pattern detection. If a quote
        is too recent, defer it to the buffer.
        """
        if self.last_quote_ts is not None:
            elapsed = ts - self.last_quote_ts
            if elapsed < self.min_jitter_s:
                self.pending_quote = True
                self.pending_quote_reason = "min_jitter"
                return False
        return True

    def get_jitter(self) -> float:
        """Get random jitter for the next quote placement."""
        return random.uniform(self.min_jitter_s, self.max_jitter_s)

    def record_quote(self, ts: float) -> None:
        """Record that a quote was placed at ts."""
        self.last_quote_ts = ts
        self.pending_quote = False


@dataclass
class AntiGamingDetector:
    """Detect when informed traders are systematically exploiting us.

    Tracks:
    - Repeated fills from the same counterparty
    - Systematic sniping after large price moves
    - Adverse selection patterns

    When gaming is detected, widen spread or skip quoting.
    """

    fill_history: deque = field(default_factory=lambda: deque(maxlen=200))
    gaming_score: float = 0.0
    threshold: float = 0.7  # above this = gaming detected
    n_fills: int = 0
    n_adverse: int = 0  # fills with negative markout

    def record_fill(
        self, counterparty: str = "unknown", markout: float = 0.0
    ) -> None:
        """Record a fill for gaming detection."""
        self.n_fills += 1
        if markout < 0:
            self.n_adverse += 1
        self.fill_history.append({
            "counterparty": counterparty,
            "markout": markout,
            "ts": len(self.fill_history),
        })
        # Update gaming score: high adverse selection rate = gaming
        if self.n_fills > 0:
            adverse_rate = self.n_adverse / self.n_fills
            # Gaming score = adverse rate weighted by recency
            self.gaming_score = (
                0.7 * self.gaming_score
                + 0.3 * adverse_rate
            )

    def is_gaming(self) -> bool:
        """True if gaming score exceeds threshold."""
        return self.gaming_score > self.threshold

    def recommended_spread_mult(self) -> float:
        """Spread multiplier when gaming is detected."""
        if not self.is_gaming():
            return 1.0
        # Widen spread proportionally to gaming score
        return 1.0 + self.gaming_score


@dataclass
class IcebergSizer:
    """Split large orders into smaller displayed chunks.

    Hides true order size from the order book to avoid detection
    and reduce market impact.
    """

    total_size: float = 0.0
    displayed_size: float = 0.0
    remaining: float = 0.0
    n_chunks: int = 0

    def plan(self, total: float, displayed: float) -> int:
        """Plan iceberg order.

        Returns number of chunks needed to fill total with displayed size.
        """
        if displayed <= 0 or total <= 0:
            self.total_size = 0.0
            self.displayed_size = 0.0
            self.remaining = 0.0
            self.n_chunks = 0
            return 0
        self.total_size = total
        self.displayed_size = displayed
        self.remaining = total
        self.n_chunks = math.ceil(total / displayed)
        return self.n_chunks

    def next_chunk(self) -> float:
        """Get the size of the next iceberg chunk.

        Returns 0 when no chunks remaining.
        """
        if self.remaining <= 0:
            return 0.0
        chunk = min(self.displayed_size, self.remaining)
        self.remaining -= chunk
        return chunk

    def progress(self) -> float:
        """Fraction of order filled (0 to 1)."""
        if self.total_size <= 0:
            return 0.0
        return 1.0 - (self.remaining / self.total_size)


@dataclass
class SmartExecutor:
    """Top-level execution intelligence.

    Combines timing, anti-gaming, and iceberg sizing to decide
    when and how to place orders.
    """

    timing: OrderTimingOptimizer = field(default_factory=OrderTimingOptimizer)
    anti_gaming: AntiGamingDetector = field(default_factory=AntiGamingDetector)
    iceberg: IcebergSizer = field(default_factory=IcebergSizer)
    n_executions: int = 0
    n_delayed: int = 0
    n_gaming_blocks: int = 0

    def should_quote(self, ts: float) -> tuple[bool, str]:
        """Decide whether to place an order now.

        Returns (should_quote, reason).
        Returns False if:
        - Within min_jitter (too soon since last quote)
        - Anti-gaming triggered (widen or skip)
        """
        if not self.timing.should_quote_now(ts):
            self.n_delayed += 1
            return False, "timing_jitter"
        if self.anti_gaming.is_gaming():
            self.n_gaming_blocks += 1
            return False, "anti_gaming"
        return True, "ok"

    def plan_iceberg(
        self, total_size: float, displayed_size: float
    ) -> int:
        """Plan an iceberg order. Returns number of chunks."""
        return self.iceberg.plan(total_size, displayed_size)

    def get_spread_multiplier(self) -> float:
        """Current spread multiplier (includes anti-gaming widening)."""
        return self.anti_gaming.recommended_spread_mult()

    def record_execution(self) -> None:
        """Record that an execution was performed."""
        self.n_executions += 1
