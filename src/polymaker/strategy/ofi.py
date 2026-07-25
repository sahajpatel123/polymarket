"""Order-Flow Imbalance (OFI) Calculator.

Reference: Cont, Kukanov, Stoikov (2014), "The Price Impact of Order Book Events"

OFI measures the net supply and demand changes at the top of the order book
between consecutive ticks k-1 and k:

    e_k^bid =   v_k^bid               if p_k^bid > p_{k-1}^bid
              v_k^bid - v_{k-1}^bid   if p_k^bid = p_{k-1}^bid
              0                       if p_k^bid < p_{k-1}^bid

    e_k^ask =   0                       if p_k^ask > p_{k-1}^ask
              v_k^ask - v_{k-1}^ask   if p_k^ask = p_{k-1}^ask
             -v_{k-1}^ask             if p_k^ask < p_{k-1}^ask

    OFI_k = e_k^bid - e_k^ask

OFI > 0 signals net buying pressure at top of book (bullish short-term tilt).
OFI < 0 signals net selling pressure at top of book (bearish short-term tilt).

Pure state machine — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from polymaker.marketdata.orderbook import BookView


@dataclass
class OFICalculator:
    """Calculates top-of-book Order-Flow Imbalance (OFI)."""

    halflife_s: float = 30.0
    _last_bid_price: float | None = None
    _last_bid_size: float = 0.0
    _last_ask_price: float | None = None
    _last_ask_size: float = 0.0
    _last_ts: float = 0.0
    _ofi_ewma: float = 0.0

    def update_from_book(self, view: BookView, ts: float) -> float:
        """Update OFI from a BookView snapshot at timestamp ts.
        
        Returns normalized OFI EWMA.
        """
        bid_p = view.best_bid
        bid_v = view.best_bid_size
        ask_p = view.best_ask
        ask_v = view.best_ask_size


        if self._last_bid_price is None or self._last_ask_price is None:
            self._last_bid_price = bid_p
            self._last_bid_size = bid_v
            self._last_ask_price = ask_p
            self._last_ask_size = ask_v
            self._last_ts = ts
            return 0.0

        # Compute e_bid
        if bid_p is None or self._last_bid_price is None:
            e_bid = 0.0
        elif bid_p > self._last_bid_price:
            e_bid = bid_v
        elif bid_p == self._last_bid_price:
            e_bid = bid_v - self._last_bid_size
        else:
            e_bid = 0.0

        # Compute e_ask
        if ask_p is None or self._last_ask_price is None:
            e_ask = 0.0
        elif ask_p > self._last_ask_price:
            e_ask = 0.0
        elif ask_p == self._last_ask_price:
            e_ask = ask_v - self._last_ask_size
        else:
            e_ask = -self._last_ask_size

        raw_ofi = e_bid - e_ask

        dt = max(0.0, ts - self._last_ts)
        decay = 0.5 ** (dt / self.halflife_s) if self.halflife_s > 0 else 0.5
        self._ofi_ewma = decay * self._ofi_ewma + (1.0 - decay) * raw_ofi

        self._last_bid_price = bid_p
        self._last_bid_size = bid_v
        self._last_ask_price = ask_p
        self._last_ask_size = ask_v
        self._last_ts = ts

        return self._ofi_ewma

    @property
    def ofi(self) -> float:
        """Current smoothed Order-Flow Imbalance."""
        return self._ofi_ewma

    @property
    def normalized_ofi(self) -> float:
        """Normalized OFI in approx [-1, +1]."""
        denom = max(1.0, self._last_bid_size + self._last_ask_size)
        return max(-1.0, min(1.0, self._ofi_ewma / denom))
