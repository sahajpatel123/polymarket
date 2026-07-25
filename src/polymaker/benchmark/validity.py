"""Mandatory validity gates so empty / broken benchmarks cannot PASS.

A financial benchmark that produces zero quotes or zero fills is
INSUFFICIENT_DATA, not success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BenchmarkStatus(str, Enum):
    PASS = "PASS"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INSUFFICIENT_CAPITAL = "INSUFFICIENT_CAPITAL"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class ValidityConfig:
    """Minimum evidence required for a financial PASS."""

    min_quotes: int = 50
    min_fills: int = 10
    min_marks: int = 20
    min_active_markets: int = 1
    min_runtime_s: float = 60.0
    min_trade_prints: int = 20
    max_missing_data_frac: float = 0.5
    # When capital cannot produce valid orders
    require_actionable_quotes: bool = True


@dataclass
class ValidityResult:
    status: BenchmarkStatus
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is BenchmarkStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


def evaluate_benchmark(
    *,
    n_quote: int = 0,
    n_fill: int = 0,
    n_mark: int = 0,
    n_markets: int = 0,
    runtime_s: float = 0.0,
    n_trade_prints: int = 0,
    missing_data_frac: float = 0.0,
    capital_ok: bool = True,
    state_divergence_events: int = 0,
    fills_after_cancel: int = 0,
    overfills: int = 0,
    cfg: ValidityConfig | None = None,
) -> ValidityResult:
    """Return PASS only when evidence thresholds and safety invariants hold."""
    c = cfg or ValidityConfig()
    reasons: list[str] = []
    metrics = {
        "n_quote": n_quote,
        "n_fill": n_fill,
        "n_mark": n_mark,
        "n_markets": n_markets,
        "runtime_s": runtime_s,
        "n_trade_prints": n_trade_prints,
        "missing_data_frac": missing_data_frac,
        "state_divergence_events": state_divergence_events,
        "fills_after_cancel": fills_after_cancel,
        "overfills": overfills,
    }

    # Hard safety failures
    if state_divergence_events > 0:
        reasons.append(f"state_divergence_events={state_divergence_events}")
    if fills_after_cancel > 0:
        reasons.append(f"fills_after_cancel={fills_after_cancel}")
    if overfills > 0:
        reasons.append(f"overfills={overfills}")
    if reasons:
        return ValidityResult(BenchmarkStatus.FAIL, reasons, metrics)

    if not capital_ok:
        return ValidityResult(
            BenchmarkStatus.INSUFFICIENT_CAPITAL,
            ["capital cannot fund one valid two-sided cycle"],
            metrics,
        )

    data_reasons: list[str] = []
    if n_quote < c.min_quotes:
        data_reasons.append(f"n_quote={n_quote} < min_quotes={c.min_quotes}")
    if n_fill < c.min_fills:
        data_reasons.append(f"n_fill={n_fill} < min_fills={c.min_fills}")
    if n_mark < c.min_marks:
        data_reasons.append(f"n_mark={n_mark} < min_marks={c.min_marks}")
    if n_markets < c.min_active_markets:
        data_reasons.append(f"n_markets={n_markets} < min={c.min_active_markets}")
    if runtime_s < c.min_runtime_s:
        data_reasons.append(f"runtime_s={runtime_s:.1f} < min={c.min_runtime_s}")
    if n_trade_prints < c.min_trade_prints:
        data_reasons.append(
            f"n_trade_prints={n_trade_prints} < min={c.min_trade_prints}"
        )
    if missing_data_frac > c.max_missing_data_frac:
        data_reasons.append(
            f"missing_data_frac={missing_data_frac:.2f} > max={c.max_missing_data_frac}"
        )
    if c.require_actionable_quotes and n_quote == 0:
        data_reasons.append("zero quotes (silent non-trading run)")
    if n_fill == 0 and n_quote > 0:
        # Quotes without fills is incomplete for financial claims
        data_reasons.append("zero fills despite quotes")

    if data_reasons:
        return ValidityResult(
            BenchmarkStatus.INSUFFICIENT_DATA, data_reasons, metrics
        )

    return ValidityResult(BenchmarkStatus.PASS, ["all gates met"], metrics)
