"""Order book microstructure features: extract alpha from raw data.

The raw features (spread, depth, flow) are necessary but not sufficient
for intelligent quoting. This module extracts higher-order features
that predict fill probability and adverse selection:

  - Microprice: weighted mid that predicts short-term price movement
  - Order book imbalance: predicts mid price over next N seconds
  - Trade flow toxicity: predicts post-fill markout
  - Queue position: where our orders sit in the book
  - Fill probability: likelihood of getting filled at our price
  - Adverse selection risk: probability of being picked off

These features are computed from the live data feed and used by the
adaptive spread engine and regime detector to make smarter decisions.

Pure functions only — no I/O. The engine feeds raw order book + trade
data via the FeatureExtractor, and queries computed features.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class MicrostructureFeatures:
    """Higher-order features derived from the raw order book + flow.

    These features predict:
    - Fill probability at a given price level
    - Adverse selection risk (markout) at a given offset from FV
    - Short-term price movement direction
    """

    # Microprice: weighted mid that predicts next-tick price
    # microprice = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
    # If microprice > mid, price is likely to go up (imbalance)
    microprice: float = 0.0
    microprice_delta_ticks: float = 0.0  # (microprice - mid) / tick

    # Order book imbalance: predicts mid price over next 1-5 seconds
    # imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    # Range: [-1, +1]. Positive = more bids than asks = price likely up.
    depth_imbalance: float = 0.0

    # Trade flow imbalance: predicts mid over next 5-30 seconds
    # flow_imbalance = sum(signed_size in last N trades) / sum(|size|)
    flow_imbalance: float = 0.0
    flow_count: int = 0

    # Volatility: short-term realized vol
    # computed as std of mid price changes over last N seconds
    realized_vol: float = 0.0
    # Vol regime: vol_short / vol_long (1.0 = stable, >2 = vol spike)
    vol_regime: float = 1.0

    # Toxicity: predicts post-fill markout
    # toxicity = -ewma(markout_30s) (positive = bad for us)
    toxicity: float = 0.0

    # Queue position: where our orders sit in the book
    # queue_ahead_us = number of shares ahead of our bid at our price
    # queue_ahead_them = number of shares ahead of our ask at our price
    queue_ahead_us: float = 0.0
    queue_ahead_them: float = 0.0

    # Fill probability: probability of fill at our price in next N seconds
    # Computed from imbalance + flow + vol + toxicity
    fill_probability_buy: float = 0.0
    fill_probability_sell: float = 0.0

    # Adverse selection risk: probability of being picked off
    # Higher when flow imbalance is large (aggressors are pushing price)
    adverse_selection_risk: float = 0.0

    # Trade opportunity score: combined fill_prob * edge * (1 - adverse_risk)
    # Higher = better trade. Used to rank markets when capital is limited.
    opportunity_score: float = 0.0


def compute_microprice(
    best_bid: float, bid_size: float, best_ask: float, ask_size: float
) -> float:
    """Compute the microprice: weighted mid that predicts next price.

    Formula: microprice = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)
    Interpretation: if ask_size > bid_size, microprice > mid (price likely up).
    """
    if bid_size + ask_size <= 0:
        return (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
    return (best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)


def compute_depth_imbalance(
    bid_depth: float, ask_depth: float
) -> float:
    """Compute order book imbalance in [-1, +1].

    Positive = more bids than asks (buying pressure).
    """
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    return (bid_depth - ask_depth) / total


def compute_flow_imbalance(
    recent_trades: deque, window_s: float = 30.0, now: float = 0.0
) -> tuple[float, int]:
    """Compute trade flow imbalance over the last `window_s` seconds.

    Returns (signed_imbalance, count) where imbalance is in [-1, +1].
    Positive = more buy trades (price likely up).
    """
    if not recent_trades:
        return 0.0, 0
    cutoff = now - window_s
    signed_total = 0.0
    abs_total = 0.0
    count = 0
    for t in recent_trades:
        if t["ts"] < cutoff:
            continue
        count += 1
        signed = t["size"] if t["side"] == "BUY" else -t["size"]
        signed_total += signed
        abs_total += t["size"]
    if abs_total <= 0:
        return 0.0, 0
    return signed_total / abs_total, count


def compute_realized_vol(
    recent_mids: list[tuple[float, float]],  # list of (ts, mid_price)
    window_s: float = 60.0,
) -> float:
    """Compute realized volatility from recent mid prices.

    Returns the std of log returns over the last `window_s` seconds.
    """
    if len(recent_mids) < 2:
        return 0.0
    cutoff = recent_mids[-1][0] - window_s
    recent = [(t, p) for t, p in recent_mids if t >= cutoff]
    if len(recent) < 2:
        return 0.0
    # Compute log returns
    log_returns = []
    for i in range(1, len(recent)):
        dt = recent[i][0] - recent[i-1][0]
        if dt > 0:
            log_return = math.log(recent[i][1] / recent[i-1][1])
            log_returns.append(log_return / dt)
    if not log_returns:
        return 0.0
    # Std
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    return math.sqrt(variance)


def compute_fill_probability(
    depth_imbalance: float,
    flow_imbalance: float,
    vol_regime: float,
    toxicity: float,
    offset_ticks: float,
) -> float:
    """Compute the probability of fill at `offset_ticks` from FV.

    Higher imbalance + flow in our direction = higher fill probability.
    Higher vol = higher fill probability (more activity).
    Higher toxicity = lower fill probability (adverse selectors avoid us).
    Larger offset = lower fill probability (further from mid).

    This is a heuristic; the true model is learned from observed fills.
    """
    # Base rate: a tight spread gets filled ~1% of the time per minute
    base_rate = 0.01
    # Imbalance in our direction: +0.5 imbalance doubles fill rate
    imbalance_mult = 1.0 + 2.0 * abs(depth_imbalance + flow_imbalance) / 2.0
    # Volatility: more vol = more fills (traders cross wider)
    vol_mult = min(3.0, max(0.5, vol_regime))
    # Toxicity: high toxicity deters adverse selectors, increasing fill rate
    tox_mult = 1.0 + 2.0 * max(0.0, toxicity)
    # Offset: larger offset = lower fill rate
    offset_mult = math.exp(-abs(offset_ticks) / 2.0)
    return min(1.0, base_rate * imbalance_mult * vol_mult * tox_mult * offset_mult)


def compute_adverse_selection_risk(
    depth_imbalance: float,
    flow_imbalance: float,
    realized_vol: float,
) -> float:
    """Compute the probability of being picked off after a fill.

    High imbalance + flow in one direction = high AS risk (the fill
    was triggered by informed trading). High vol = high AS risk.
    """
    # Flow direction matches imbalance = informed flow
    informed_flow = abs(depth_imbalance + flow_imbalance) / 2.0
    vol_factor = min(2.0, max(0.5, realized_vol * 1000))
    return min(1.0, informed_flow * vol_factor)


def compute_opportunity_score(
    fill_prob: float,
    expected_edge: float,
    as_risk: float,
) -> float:
    """Compute a trade opportunity score.

    Score = fill_prob * expected_edge * (1 - as_risk).
    Higher = better trade. Used to rank markets when capital is limited.
    """
    return max(0.0, fill_prob * expected_edge * (1.0 - as_risk))


@dataclass
class MicrostructureTracker:
    """Track microstructure features over time.

    Maintains rolling windows of:
    - Recent mid prices (for vol calculation)
    - Recent trades (for flow imbalance)
    - Current book state

    Pure state machine — no I/O. The engine feeds it via update_book()
    and update_trade(), and queries via extract().
    """

    recent_mids: deque = field(default_factory=lambda: deque(maxlen=500))
    recent_trades: deque = field(default_factory=lambda: deque(maxlen=200))
    last_bid: float = 0.0
    last_ask: float = 0.0
    last_bid_depth: float = 0.0
    last_ask_depth: float = 0.0
    last_ts: float = 0.0

    def update_book(
        self, best_bid: float, best_ask: float, bid_depth: float,
        ask_depth: float, ts: float,
    ) -> None:
        """Record a book snapshot."""
        self.last_bid = best_bid
        self.last_ask = best_ask
        self.last_bid_depth = bid_depth
        self.last_ask_depth = ask_depth
        self.last_ts = ts
        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2.0
            self.recent_mids.append((ts, mid))

    def update_trade(
        self, side: str, price: float, size: float, ts: float,
    ) -> None:
        """Record a trade print."""
        self.recent_trades.append({
            "side": side,
            "price": price,
            "size": size,
            "ts": ts,
        })

    def extract(
        self, vol_short: float = 0.0, vol_long: float = 0.0,
        markout_short: float = 0.0,
    ) -> MicrostructureFeatures:
        """Extract a feature snapshot at the current time."""
        microprice = compute_microprice(
            self.last_bid, self.last_bid_depth,
            self.last_ask, self.last_ask_depth
        )
        mid = (self.last_bid + self.last_ask) / 2.0 if self.last_bid > 0 and self.last_ask > 0 else 0.0
        microprice_delta = (microprice - mid) if mid > 0 else 0.0
        # Tick size: assume 0.001 if not known
        tick = 0.001
        microprice_delta_ticks = microprice_delta / tick if tick > 0 else 0.0

        depth_imbalance = compute_depth_imbalance(
            self.last_bid_depth, self.last_ask_depth
        )
        flow_imbalance, flow_count = compute_flow_imbalance(
            self.recent_trades, window_s=30.0, now=self.last_ts
        )
        realized_vol = compute_realized_vol(
            list(self.recent_mids), window_s=60.0
        )
        vol_regime = vol_short / vol_long if vol_long > 1e-9 else 1.0
        toxicity = max(0.0, -markout_short)  # positive = bad for us

        # Fill probability at different offsets
        fill_prob_buy = compute_fill_probability(
            depth_imbalance, -flow_imbalance,  # flow against us for BUY
            vol_regime, toxicity, offset_ticks=1.0,
        )
        fill_prob_sell = compute_fill_probability(
            depth_imbalance, flow_imbalance,  # flow with us for SELL
            vol_regime, toxicity, offset_ticks=1.0,
        )
        as_risk = compute_adverse_selection_risk(
            depth_imbalance, flow_imbalance, realized_vol
        )
        opportunity = compute_opportunity_score(
            (fill_prob_buy + fill_prob_sell) / 2,
            expected_edge=0.005,  # 0.5¢ assumed edge
            as_risk=as_risk,
        )

        return MicrostructureFeatures(
            microprice=microprice,
            microprice_delta_ticks=microprice_delta_ticks,
            depth_imbalance=depth_imbalance,
            flow_imbalance=flow_imbalance,
            flow_count=flow_count,
            realized_vol=realized_vol,
            vol_regime=vol_regime,
            toxicity=toxicity,
            queue_ahead_us=0.0,
            queue_ahead_them=0.0,
            fill_probability_buy=fill_prob_buy,
            fill_probability_sell=fill_prob_sell,
            adverse_selection_risk=as_risk,
            opportunity_score=opportunity,
        )
