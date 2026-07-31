"""Unified per-market decision — regime + band position + fill learning.

Priority order, highest first:
  HALTED       kill switch / stale data / resolved / past halt-before window
  EVENT        active cooloff, or a fresh sweep / fair-value jump
  REDUCE_ONLY  inventory at hard cap, or inside the reduce-only end-date window
  TRENDING     persistent one-sided flow or elevated short/long vol
  QUIET        default farming posture

The same machine owns fill-learning for band positioning, creating a single
Bayesian posterior P(regime, optimal_band | features) instead of cascading
independent heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polymaker.config import StrategyProfile
from polymaker.domain import Regime


@dataclass(frozen=True, slots=True)
class RegimeInputs:
    now: float
    tick: float
    fv: float
    prev_fv: float | None
    vol_ratio: float
    flow_z: float
    inventory_util: float  # |net notional| / q_max, >=0
    hours_to_end: float | None
    sweep_flagged: bool = False
    market_resolved: bool = False
    ws_stale: bool = False
    risk_halt: bool = False
    risk_reduce_only: bool = False
    # Fill-based learning
    toxicity: float = 0.0
    n_fills_last_hour: int = 0
    seconds_since_last_trade: float = 120.0


@dataclass
class _FillBin:
    """Statistics for one offset bin (ticks from FV)."""
    n_quotes: int = 0
    n_fills: int = 0
    sum_edge: float = 0.0


class RegimeMachine:
    """Unified regime decider + band-position learner for one market.

    Produces a regime decision *and* a recommended band position from
    the same posterior. The band position learns from fill outcomes
    and is modulated by the current regime (toxic/trending → more passive).
    """

    __slots__ = ("_event_until", "_fill_bins", "_n_fills", "_n_quotes",
                 "_best_offset", "_best_fill_rate")

    def __init__(self) -> None:
        self._event_until: float = 0.0
        self._fill_bins: dict[int, _FillBin] = {}
        self._n_fills: int = 0
        self._n_quotes: int = 0
        self._best_offset: int = 3
        self._best_fill_rate: float = 0.0

    # ── regime decision (unchanged priority logic) ─────────────────────

    def decide(self, inp: RegimeInputs, p: StrategyProfile) -> Regime:
        if inp.risk_halt or inp.ws_stale or inp.market_resolved:
            return Regime.HALTED
        if inp.hours_to_end is not None and inp.hours_to_end <= p.halt_before_hours:
            return Regime.HALTED
        jump_ticks = abs(inp.fv - inp.prev_fv) / inp.tick if inp.prev_fv is not None else 0.0
        if inp.sweep_flagged or jump_ticks >= p.event_jump_ticks:
            self._event_until = inp.now + p.event_cooloff_s
            return Regime.EVENT
        if inp.now < self._event_until:
            return Regime.EVENT
        if inp.risk_reduce_only or inp.inventory_util >= 1.0:
            return Regime.REDUCE_ONLY
        if inp.hours_to_end is not None and inp.hours_to_end <= p.reduce_only_hours:
            return Regime.REDUCE_ONLY
        flow_hit = abs(inp.flow_z) >= p.trend_flow_z
        vol_strong = inp.vol_ratio >= p.trend_vol_ratio * 1.5
        vol_with_flow = (
            inp.vol_ratio >= p.trend_vol_ratio
            and abs(inp.flow_z) >= 0.5 * p.trend_flow_z
        )
        if flow_hit or vol_strong or vol_with_flow:
            return Regime.TRENDING
        return Regime.QUIET

    @property
    def in_cooloff(self) -> bool:
        return self._event_until > 0.0

    def cooloff_remaining(self, now: float) -> float:
        return max(0.0, self._event_until - now)

    # ── fill learning (band-position posterior) ────────────────────────

    def record_quote(self, offset_ticks: int) -> None:
        self._n_quotes += 1
        if offset_ticks not in self._fill_bins:
            self._fill_bins[offset_ticks] = _FillBin()
        self._fill_bins[offset_ticks].n_quotes += 1

    def record_fill(self, offset_ticks: int, edge: float, markout: float) -> None:
        self._n_fills += 1
        if offset_ticks not in self._fill_bins:
            self._fill_bins[offset_ticks] = _FillBin()
        bin_ = self._fill_bins[offset_ticks]
        bin_.n_fills += 1
        bin_.sum_edge += edge
        self._update_best_offset()

    def _update_best_offset(self) -> None:
        best_off = self._best_offset
        best_rate = self._best_fill_rate
        for off, bin_ in self._fill_bins.items():
            if bin_.n_quotes == 0 or bin_.n_fills == 0:
                continue
            rate = bin_.n_fills / bin_.n_quotes
            avg_edge = bin_.sum_edge / bin_.n_fills
            if avg_edge <= 0:
                continue
            score = rate * avg_edge
            if score > best_rate * 0.001:  # minimal initial threshold
                best_off = off
                best_rate = rate
        if best_rate > 0:
            self._best_offset = best_off
            self._best_fill_rate = best_rate

    def suggest_band_position(self, regime: Regime, toxicity: float,
                              default_ticks: int = 3) -> tuple[float, bool]:
        """Return (buy_band_frac, should_skip_quoting).

        band_frac: 0.0 = passive (band floor), 1.0 = aggressive (near FV).
        skip: True when toxicity/fill data suggest not quoting at all.

        Combines regime + fill learning + toxicity into a single posterior
        for the optimal band position. Toxic/trending regimes push toward
        the band floor; QUIET with good fills pushes toward the mid-band.
        """
        if regime in (Regime.HALTED, Regime.EVENT):
            return 0.0, True
        if regime is Regime.REDUCE_ONLY:
            return 0.5, False

        # Start from learned best offset
        if self._best_fill_rate > 0 and self._n_fills > 0:
            frac = min(1.0, self._best_offset / max(default_ticks * 2, 1))
        else:
            frac = 0.5  # mid-band when no data

        # Toxicity: push toward floor
        if toxicity > 0.1:
            frac = min(frac, max(0.1, 0.5 - toxicity))
        if toxicity > 0.05:
            frac = min(frac, 0.4)

        # TRENDING: more passive
        if regime is Regime.TRENDING:
            frac = min(frac, 0.3)

        # Skip if very toxic and no fill history
        skip = toxicity > 0.2 and self._n_fills == 0

        return max(0.0, min(1.0, frac)), skip
