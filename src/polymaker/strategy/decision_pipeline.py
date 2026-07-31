"""Shared decision → target pipeline for live, paper, and replay.

One function builds TargetQuotes + attribution from market state.
Only I/O adapters (engine gateway vs FillSimulator) should differ.

Market event
  → estimators (caller updated)
  → intelligence features / decision
  → regime
  → quote construction
  → (caller) reconciliation + execution
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Quote, Regime, Side, TargetQuotes
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.estimators import MarketEstimators
from polymaker.strategy.quoting import QuoteInputs, compute_fair_value, construct_quotes
from polymaker.strategy.regime import RegimeInputs, RegimeMachine


def _empty_view() -> BookView:
    return BookView(None, 0.0, None, 0.0, None, None, 0.0, 0.0)


@dataclass
class QuoteAttribution:
    """Why this quote set was produced — for intel-on vs off comparison."""

    fair_value: float
    regime: str
    intelligence_decision: str  # QUOTE | SKIP | OFF
    buy_offset_ticks: int = 0
    sell_offset_ticks: int = 0
    size_multiplier: float = 1.0
    spread_multiplier: float = 1.0
    risk_multiplier: float = 1.0
    buy_band_frac: float | None = None
    reason_codes: list[str] = field(default_factory=list)
    intel_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fair_value": self.fair_value,
            "regime": self.regime,
            "intelligence_decision": self.intelligence_decision,
            "buy_offset_ticks": self.buy_offset_ticks,
            "sell_offset_ticks": self.sell_offset_ticks,
            "size_multiplier": self.size_multiplier,
            "spread_multiplier": self.spread_multiplier,
            "risk_multiplier": self.risk_multiplier,
            "buy_band_frac": self.buy_band_frac,
            "reason_codes": list(self.reason_codes),
            "intel_reason": self.intel_reason,
        }


@dataclass
class PipelineResult:
    targets: TargetQuotes
    attribution: QuoteAttribution
    fv: float
    regime: Regime
    yes_view: BookView


def build_targets(
    *,
    meta: MarketMeta,
    profile: StrategyProfile,
    yes_view: BookView,
    no_view: BookView | None,
    pos_yes: Position,
    pos_no: Position,
    est: MarketEstimators,
    regime_machine: RegimeMachine,
    now: float,
    micro: float | None = None,
    risk_size_scale: float = 1.0,
    risk_halt: bool = False,
    risk_reduce_only: bool = False,
    hours_to_end: float | None = None,
    sweep_flagged: bool = False,
    ws_stale: bool = False,
    market_resolved: bool = False,
    intel: Any | None = None,
    n_trades_last_hour: int = 0,
    seconds_since_last_trade: float = 0.0,
    yes_exit_urgency: float = 0.0,
    no_exit_urgency: float = 0.0,
) -> PipelineResult | None:
    """Pure decision pipeline. Returns None if book is unusable.

    Caller owns estimator updates (flow, on_fair_value). This function only
    reads estimators and produces targets + attribution.
    """
    if yes_view.best_bid is None or yes_view.best_ask is None:
        return None
    if yes_view.best_bid >= yes_view.best_ask:
        return None

    p = profile
    tick = meta.tick_size
    mid = 0.5 * (yes_view.best_bid + yes_view.best_ask)
    mprice = float(micro) if micro is not None else mid
    est.flow.decay_to(now)
    fv = compute_fair_value(mprice, est.flow.z, tick, weight=p.flow_fv_weight)
    prev_fv = est.last_fv

    q_max = p.q_max_usdc
    inv_util = (
        abs(pos_yes.size - pos_no.size) * fv / q_max if q_max > 0 else 0.0
    )
    regime = regime_machine.decide(
        RegimeInputs(
            now=now,
            tick=tick,
            fv=fv,
            prev_fv=prev_fv,
            vol_ratio=est.vol.ratio,
            flow_z=est.flow.z,
            inventory_util=inv_util,
            hours_to_end=hours_to_end,
            sweep_flagged=sweep_flagged,
            ws_stale=ws_stale,
            risk_halt=risk_halt,
            risk_reduce_only=risk_reduce_only,
            market_resolved=market_resolved,
            toxicity=float(getattr(est.markout, "toxicity", 0.0) or 0.0),
            n_fills_last_hour=int(n_trades_last_hour),
            seconds_since_last_trade=float(seconds_since_last_trade),
        ),
        p,
    )

    nv = no_view if no_view is not None else _empty_view()
    reasons: list[str] = [f"regime_{regime.value.lower()}"]

    intel_skip = False
    intel_size = 1.0
    intel_band: float | None = None
    intel_spread = 1.0
    intel_buy_off: int | None = None
    intel_reason = ""
    intel_decision = "OFF"

    # Unified regime machine owns band-position learning (fill stats +
    # toxicity regime). Always consulted when use_intelligence is True.
    tox = float(getattr(est.markout, "toxicity", 0.0) or 0.0)
    if bool(getattr(p, "use_intelligence", False)) and regime not in (Regime.HALTED, Regime.EVENT):
        band_frac, should_skip = regime_machine.suggest_band_position(
            regime, tox, default_ticks=p.delta_min_ticks,
        )
        intel_skip = should_skip
        intel_band = band_frac
        intel_reason = f"unified:band={band_frac:.2f},tox={tox:.2f}"
        intel_decision = "SKIP" if intel_skip else "QUOTE"
        if intel_skip:
            reasons.append("intel_skip")
        else:
            reasons.append(f"band_frac_{band_frac:.2f}")

    tq = construct_quotes(
        QuoteInputs(
            meta=meta,
            regime=regime,
            fv=fv,
            vol_short=est.vol.short,
            toxicity=est.markout.toxicity,
            yes_view=yes_view,
            no_view=nv,
            pos_yes=pos_yes,
            pos_no=pos_no,
            profile=p,
            now=now,
            risk_size_scale=risk_size_scale,
            kyle_lambda=float(getattr(est.kyle, "lambda_param", 0.0) or 0.0),
            intel_size_scale=intel_size,
            intel_buy_band_frac=intel_band,
            intel_spread_mult=intel_spread,
            intel_buy_offset_ticks=intel_buy_off,
            intel_skip=intel_skip,
            yes_exit_urgency=yes_exit_urgency,
            no_exit_urgency=no_exit_urgency,
            kelly_fraction=float(getattr(p, "kelly_fraction", 0.0) or 0.0),
            bankroll_usdc=max(
                float(getattr(p, "bankroll_usdc", 0.0) or 0.0),
                float(getattr(meta, "liquidity_num", 0) or 0) * 0.02,
            ),
            liquidity=float(getattr(meta, "liquidity_num", 0) or 0),
        )
    )

    attr = QuoteAttribution(
        fair_value=fv,
        regime=regime.value,
        intelligence_decision=intel_decision,
        buy_offset_ticks=int(intel_buy_off or 0),
        size_multiplier=intel_size,
        spread_multiplier=intel_spread,
        risk_multiplier=risk_size_scale,
        buy_band_frac=intel_band,
        reason_codes=reasons,
        intel_reason=intel_reason,
    )
    return PipelineResult(
        targets=tq, attribution=attr, fv=fv, regime=regime, yes_view=yes_view
    )
