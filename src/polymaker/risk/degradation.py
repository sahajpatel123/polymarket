"""Live degradation detector: auto retreat when edge disappears.

Compares rolling markout / fill quality / error signals to control limits.
When degraded, recommends size cut, baseline fallback, market quarantine,
or global halt — more valuable than adding another predictor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DegradationAction(str, Enum):
    NONE = "none"
    SIZE_CUT = "size_cut"
    BASELINE_FALLBACK = "baseline_fallback"
    MARKET_QUARANTINE = "market_quarantine"
    GLOBAL_HALT = "global_halt"


@dataclass
class DegradationDecision:
    action: DegradationAction
    size_multiplier: float = 1.0  # apply on top of strategy size
    use_baseline_profile: bool = False
    quarantine: bool = False
    halt: bool = False
    reason: str = ""
    confidence: float = 1.0


@dataclass
class DegradationConfig:
    # Rolling windows
    min_fills_for_signal: int = 20
    # Mean markout (price units) below this → toxic
    markout_toxic: float = -0.01
    markout_warn: float = -0.003
    # Rolling fill rate collapse
    fill_rate_floor: float = 0.02
    # Error / reject rate
    order_error_rate_halt: float = 0.25
    # Drawdown vs day-start equity
    drawdown_halt_frac: float = 0.10
    drawdown_cut_frac: float = 0.05
    # Size cut levels
    size_cut_mild: float = 0.5
    size_cut_hard: float = 0.25


@dataclass
class DegradationState:
    """Rolling observations for one market or global book."""

    n_fills: int = 0
    sum_markout: float = 0.0
    n_quotes: int = 0
    n_order_errors: int = 0
    n_order_attempts: int = 0
    day_start_equity: float = 0.0
    equity: float = 0.0
    consecutive_toxic_fills: int = 0

    def record_fill(self, markout: float) -> None:
        self.n_fills += 1
        self.sum_markout += markout
        if markout < -0.005:
            self.consecutive_toxic_fills += 1
        else:
            self.consecutive_toxic_fills = 0

    def record_quote(self) -> None:
        self.n_quotes += 1

    def record_order_result(self, ok: bool) -> None:
        self.n_order_attempts += 1
        if not ok:
            self.n_order_errors += 1

    @property
    def mean_markout(self) -> float:
        if self.n_fills <= 0:
            return 0.0
        return self.sum_markout / self.n_fills

    @property
    def fill_rate(self) -> float:
        if self.n_quotes <= 0:
            return 0.0
        return self.n_fills / self.n_quotes

    @property
    def error_rate(self) -> float:
        if self.n_order_attempts < 10:
            return 0.0
        return self.n_order_errors / self.n_order_attempts

    @property
    def drawdown_frac(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        dd = self.day_start_equity - self.equity
        return max(0.0, dd / self.day_start_equity)


class DegradationDetector:
    """Evaluate whether to cut size, fall back to baseline, quarantine, or halt."""

    def __init__(self, cfg: DegradationConfig | None = None) -> None:
        self.cfg = cfg or DegradationConfig()
        self.global_state = DegradationState()
        self.markets: dict[str, DegradationState] = field(default_factory=dict)  # type: ignore
        self.markets = {}

    def state_for(self, condition_id: str) -> DegradationState:
        if condition_id not in self.markets:
            self.markets[condition_id] = DegradationState()
        return self.markets[condition_id]

    def evaluate(
        self,
        condition_id: str | None = None,
        *,
        intelligence_confidence: float = 1.0,
    ) -> DegradationDecision:
        cfg = self.cfg
        st = self.state_for(condition_id) if condition_id else self.global_state
        # Merge global drawdown/error with per-market markout
        g = self.global_state

        # 1. Hard halt
        if g.error_rate >= cfg.order_error_rate_halt and g.n_order_attempts >= 20:
            return DegradationDecision(
                DegradationAction.GLOBAL_HALT,
                size_multiplier=0.0,
                halt=True,
                reason=f"order_error_rate={g.error_rate:.2f}",
            )
        if g.drawdown_frac >= cfg.drawdown_halt_frac and g.day_start_equity > 0:
            return DegradationDecision(
                DegradationAction.GLOBAL_HALT,
                size_multiplier=0.0,
                halt=True,
                reason=f"drawdown_frac={g.drawdown_frac:.2f}",
            )

        # 2. Market quarantine on sustained toxic markouts
        if st.n_fills >= cfg.min_fills_for_signal and st.mean_markout <= cfg.markout_toxic:
            return DegradationDecision(
                DegradationAction.MARKET_QUARANTINE,
                size_multiplier=0.0,
                quarantine=True,
                reason=f"mean_markout={st.mean_markout:.4f}<=toxic",
            )
        if st.consecutive_toxic_fills >= 8:
            return DegradationDecision(
                DegradationAction.MARKET_QUARANTINE,
                size_multiplier=0.0,
                quarantine=True,
                reason=f"consecutive_toxic_fills={st.consecutive_toxic_fills}",
            )

        # 3. Baseline fallback when intelligence confidence collapses + mild AS
        if intelligence_confidence < 0.3 and st.n_fills >= 5 and st.mean_markout < cfg.markout_warn:
            return DegradationDecision(
                DegradationAction.BASELINE_FALLBACK,
                size_multiplier=cfg.size_cut_mild,
                use_baseline_profile=True,
                reason=f"low_intel_conf={intelligence_confidence:.2f}",
                confidence=intelligence_confidence,
            )

        # 4. Size cuts
        if g.drawdown_frac >= cfg.drawdown_cut_frac and g.day_start_equity > 0:
            return DegradationDecision(
                DegradationAction.SIZE_CUT,
                size_multiplier=cfg.size_cut_hard,
                reason=f"drawdown_cut={g.drawdown_frac:.2f}",
            )
        if st.n_fills >= cfg.min_fills_for_signal and st.mean_markout <= cfg.markout_warn:
            return DegradationDecision(
                DegradationAction.SIZE_CUT,
                size_multiplier=cfg.size_cut_mild,
                reason=f"markout_warn={st.mean_markout:.4f}",
            )
        if st.n_quotes >= 50 and st.fill_rate < cfg.fill_rate_floor and st.n_fills < 3:
            # Dead quoting — not toxic, but reduce churn
            return DegradationDecision(
                DegradationAction.SIZE_CUT,
                size_multiplier=cfg.size_cut_mild,
                reason=f"low_fill_rate={st.fill_rate:.3f}",
            )

        return DegradationDecision(DegradationAction.NONE, reason="healthy")
