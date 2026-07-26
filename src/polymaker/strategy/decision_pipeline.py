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
from polymaker.intelligence import DecisionFramework, MarketFeatures
from polymaker.marketdata.orderbook import BookView
from polymaker.strategy.advanced_quoting import (
    AdvancedQuoteInputs,
    compute_advanced_quotes,
)
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
    intel: DecisionFramework | None = None,
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
    intel_sell_off = 0
    intel_reason = ""
    intel_decision = "OFF"

    mode = str(getattr(p, "intelligence_mode", "full") or "full")
    use_intel = (
        bool(getattr(p, "use_intelligence", False))
        and intel is not None
        and mode != "off"
    )
    if use_intel and regime not in (Regime.HALTED, Regime.EVENT):
        tox = float(getattr(est.markout, "toxicity", 0.0) or 0.0)
        secs = float(seconds_since_last_trade)
        if n_trades_last_hour == 0 and meta.rewards_daily_rate > 0:
            secs = 0.0
        feats = MarketFeatures(
            best_bid=float(yes_view.best_bid or 0.0),
            best_ask=float(yes_view.best_ask or 0.0),
            mid_price=fv,
            bid_depth=float(yes_view.best_bid_size or 0.0),
            ask_depth=float(yes_view.best_ask_size or 0.0),
            flow_z=float(est.flow.z),
            vol_short=float(est.vol.short),
            vol_long=float(getattr(est.vol, "long", est.vol.short) or est.vol.short),
            vol_ratio=float(est.vol.ratio),
            toxicity=tox,
            markout_short=-tox,
            seconds_since_last_update=secs,
            n_trades_last_hour=int(n_trades_last_hour),
            rewards_daily_rate=float(meta.rewards_daily_rate or 0.0),
            reward_band_cents=float(meta.rewards_max_spread or 0.0),
        )
        cid = meta.condition_id
        intel.update_features(cid, feats)
        intel.update_microstructure(
            cid,
            float(yes_view.best_bid or 0.0),
            float(yes_view.best_ask or 0.0),
            float(yes_view.best_bid_size or 0.0),
            float(yes_view.best_ask_size or 0.0),
            now,
        )
        decision = intel.decide(cid)
        if mode == "gate_only":
            # Only skip dead/stale; ignore size/band learning
            intel_skip = not decision.should_quote
            intel_decision = "SKIP" if intel_skip else "QUOTE"
            intel_reason = decision.reason
            if intel_skip:
                reasons.append("intel_gate_skip")
        else:
            intel_skip = not decision.should_quote
            intel_size = float(decision.size_multiplier)
            intel_band = float(decision.buy_band_frac)
            intel_spread = max(1.0, float(decision.spread_multiplier))
            intel_buy_off = int(decision.buy_offset_ticks)
            intel_sell_off = int(decision.sell_offset_ticks)
            intel_reason = decision.reason
            intel_decision = "SKIP" if intel_skip else "QUOTE"
            if intel_skip:
                reasons.append("intel_skip")
            else:
                reasons.append(f"band_frac_{intel_band:.2f}")
                intel.record_quote(cid, decision.buy_offset_ticks)

    use_adv = (
        p.use_advanced_quoting
        and regime is not Regime.REDUCE_ONLY
        and not intel_skip
    )
    tq: TargetQuotes
    if use_adv:
        bankroll = p.bankroll_usdc if p.bankroll_usdc > 0 else p.q_max_usdc
        tox = float(getattr(est.markout, "toxicity", 0.0) or 0.0)
        adv = compute_advanced_quotes(
            AdvancedQuoteInputs(
                meta=meta,
                fv=fv,
                sigma=est.vol.short,
                yes_view=yes_view,
                no_view=nv,
                pos_yes=pos_yes,
                pos_no=pos_no,
                profile=p,
                bankroll_usdc=float(bankroll),
                now=now,
                regime=regime,
                toxicity=tox,
                risk_size_scale=risk_size_scale * intel_size,
            )
        )
        adv_quotes: list[Quote] = []
        yes_price = adv.bid
        no_price = 1.0 - adv.ask
        if adv.size_yes_shares > 0 and 0 < yes_price < 1:
            adv_quotes.append(
                Quote(
                    token_id=meta.yes.token_id,
                    side=Side.BUY,
                    price=yes_price,
                    size=adv.size_yes_shares,
                )
            )
        if adv.size_no_shares > 0 and 0 < no_price < 1:
            adv_quotes.append(
                Quote(
                    token_id=meta.no.token_id,
                    side=Side.BUY,
                    price=no_price,
                    size=adv.size_no_shares,
                )
            )
        tq = TargetQuotes(meta.condition_id, regime, tuple(adv_quotes))
        reasons.append("advanced_quoting")
    else:
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
            )
        )

    attr = QuoteAttribution(
        fair_value=fv,
        regime=regime.value,
        intelligence_decision=intel_decision,
        buy_offset_ticks=int(intel_buy_off or 0),
        sell_offset_ticks=intel_sell_off,
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
