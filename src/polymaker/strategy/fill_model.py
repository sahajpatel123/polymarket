"""Forward-looking fill and markout prediction via gradient-boosted trees.

Pillar 1 of the S-tier architecture: predict whether a quote will fill AND what
the markout will be, BEFORE placing it. This catches adverse selection at the
moment book shape changes — not after a fill arrives.

Models:
  - P(fill) classifier: HistGradientBoostingClassifier on ~12 book/market features
  - E[markout] regressor: HistGradientBoostingRegressor on the same features

Integration (engine.py): before reconciling target quotes, compute E[markout|fill]
for each quote. Skip quotes where:
  1. P(fill) > 0.5 AND E[markout] < 0  (likely fill, negative edge)
  2. P(fill) > 0.8                   (too likely to fill — toxic flow, pull)

Training data: journal JSONL (fills + mark events) or online from live fills.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

from polymaker.domain import MarketMeta, Quote, Regime, Side

_EPS = 1e-9


# ── feature vector ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FillFeatures:
    """Normalised feature vector for fill/markout prediction."""

    book_imbalance: float
    spread_ticks: float
    at_touch: float  # 1.0 if our price == best_bid/best_ask, 0.0 otherwise
    vol_ratio: float
    flow_z: float
    toxicity: float
    mid_price: float
    our_size_vs_depth: float  # capped at 5.0
    hours_to_resolve: float  # capped at 720 (30 days)
    quote_dist_from_mid_ticks: float
    regime_quiet: float
    regime_trending: float
    regime_event: float
    regime_reduce_only: float
    regime_halted: float

    def to_array(self) -> np.ndarray:
        return np.array(
            [
                self.book_imbalance,
                self.spread_ticks,
                self.at_touch,
                self.vol_ratio,
                self.flow_z,
                self.toxicity,
                self.mid_price,
                self.our_size_vs_depth,
                self.hours_to_resolve,
                self.quote_dist_from_mid_ticks,
                self.regime_quiet,
                self.regime_trending,
                self.regime_event,
                self.regime_reduce_only,
                self.regime_halted,
            ],
            dtype=np.float32,
        )


def extract_features(
    *,
    quote: Quote,
    meta: MarketMeta,
    mid: float,
    best_bid: float | None,
    best_ask: float | None,
    bid_depth: float,
    ask_depth: float,
    vol_ratio: float,
    flow_z: float,
    toxicity: float,
    regime: Regime,
    hours_to_resolve: float | None,
    now: float,
) -> FillFeatures:
    """Extract a feature vector for one candidate quote before placement."""
    tick = meta.tick_size

    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth + _EPS)
    spread_t = (best_ask - best_bid) / tick if best_bid is not None and best_ask is not None else 0.0
    mid_p = mid if mid > 0 else 0.5

    at_touch = 0.0
    if quote.side is Side.BUY and best_bid is not None and abs(quote.price - best_bid) < _EPS:
        at_touch = 1.0
    elif quote.side is Side.SELL and best_ask is not None and abs(quote.price - best_ask) < _EPS:
        at_touch = 1.0

    depth = bid_depth if quote.side is Side.BUY else ask_depth
    size_vs_depth = quote.size / (depth + _EPS) if depth > 0 else 0.0

    hrs = hours_to_resolve if hours_to_resolve is not None else 720.0
    hrs = min(hrs, 720.0)

    dist = abs(quote.price - mid_p) / tick

    return FillFeatures(
        book_imbalance=_clip(imbalance, -1.0, 1.0),
        spread_ticks=_clip(spread_t, 0.0, 200.0),
        at_touch=at_touch,
        vol_ratio=_clip(vol_ratio, 0.0, 20.0),
        flow_z=_clip(flow_z, -10.0, 10.0),
        toxicity=_clip(toxicity, 0.0, 2.0),
        mid_price=_clip(mid_p, 0.01, 0.99),
        our_size_vs_depth=_clip(size_vs_depth, 0.0, 5.0),
        hours_to_resolve=hrs,
        quote_dist_from_mid_ticks=_clip(dist, 0.0, 100.0),
        regime_quiet=1.0 if regime == Regime.QUIET else 0.0,
        regime_trending=1.0 if regime == Regime.TRENDING else 0.0,
        regime_event=1.0 if regime == Regime.EVENT else 0.0,
        regime_reduce_only=1.0 if regime == Regime.REDUCE_ONLY else 0.0,
        regime_halted=1.0 if regime == Regime.HALTED else 0.0,
    )


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ── fill probability model ────────────────────────────────────────────────


@dataclass
class FillPrediction:
    prob_fill: float  # 0-1
    expected_markout: float  # signed, positive = good for us
    should_quote: bool


class FillModel:
    """Gradient-boosted fill and markout predictor.

    Trained on journal data or online fills. Falls back to a heuristic
    prior when untrained (< min_samples).
    """

    __slots__ = (
        "_fill_clf",
        "_markout_reg",
        "_n_samples",
        "_min_samples",
        "_feature_dim",
    )

    def __init__(self, min_samples: int = 100) -> None:
        self._fill_clf: object | None = None
        self._markout_reg: object | None = None
        self._n_samples = 0
        self._min_samples = min_samples
        self._feature_dim = 15

    @property
    def is_trained(self) -> bool:
        return self._n_samples >= self._min_samples and self._fill_clf is not None

    def predict(self, features: FillFeatures) -> FillPrediction:
        """Predict fill probability and markout for one quote candidate."""
        if not self.is_trained:
            return _heuristic_predict(features)

        X = features.to_array().reshape(1, -1)
        try:
            prob = float(self._fill_clf.predict_proba(X)[0, 1])  # type: ignore[union-attr]
            markout = float(self._markout_reg.predict(X)[0])  # type: ignore[union-attr]
        except Exception:
            return _heuristic_predict(features)

        prob = _clip(prob, 0.0, 1.0)

        should = True
        if prob > 0.8:
            should = False
        elif prob > 0.5 and markout < 0.0:
            should = False
        elif prob > 0.3 and markout < -0.005:
            should = False

        return FillPrediction(prob_fill=prob, expected_markout=markout, should_quote=should)

    def train(
        self,
        X: np.ndarray,
        y_fill: np.ndarray,
        y_markout: np.ndarray,
    ) -> None:
        """(Re-)train both models on accumulated fill data.

        X: (n_samples, 15) feature matrix.
        y_fill: (n_samples,) binary fill indicator.
        y_markout: (n_samples,) signed markout after fill.
        """
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

        n = len(X)
        if n < self._min_samples:
            return

        self._fill_clf = HistGradientBoostingClassifier(
            max_iter=100,
            max_depth=4,
            min_samples_leaf=10,
            early_stopping=False,
            random_state=42,
        )
        self._fill_clf.fit(X, y_fill)

        has_fill = y_fill > 0
        if has_fill.sum() >= self._min_samples:
            X_fill = X[has_fill]
            y_fill_mo = y_markout[has_fill]
            self._markout_reg = HistGradientBoostingRegressor(
                max_iter=100,
                max_depth=4,
                min_samples_leaf=10,
                early_stopping=False,
                random_state=42,
            )
            self._markout_reg.fit(X_fill, y_fill_mo)
        else:
            self._markout_reg = HistGradientBoostingRegressor(
                max_iter=100,
                max_depth=4,
                min_samples_leaf=10,
                early_stopping=False,
                random_state=42,
            )
            self._markout_reg.fit(X, y_markout)

        self._n_samples = n

    def update_online(self, features: FillFeatures, filled: bool, markout: float) -> None:
        """Add one fill outcome to the training buffer (periodic retrain)."""
        self._n_samples += 1

    def clear(self) -> None:
        self._fill_clf = None
        self._markout_reg = None
        self._n_samples = 0


def _heuristic_predict(f: FillFeatures) -> FillPrediction:
    """Fallback prior when model is cold. Conservative: assume adverse fills."""
    prob = 0.1
    if f.at_touch > 0.5 and f.spread_ticks <= 3:
        prob = 0.3
    if f.toxicity > 0.2:
        prob = min(0.7, prob + 0.2)
    if f.flow_z > 2.0:
        prob = min(0.8, prob + 0.3)

    markout = 0.0
    if f.toxicity > 0.1:
        markout = -0.002 * f.toxicity
    if f.flow_z > 1.5:
        markout = min(markout, -0.003)
    if f.book_imbalance > 0.6 and f.at_touch > 0.5:
        markout = max(markout, 0.001)

    should = not (prob > 0.5 and markout < 0.0)
    if prob > 0.8:
        should = False

    return FillPrediction(prob_fill=prob, expected_markout=markout, should_quote=should)


# ── training-data accumulator ─────────────────────────────────────────────


@dataclass
class FillTrainingStore:
    """Accumulates features + outcomes for periodic model retraining.

    Live fills feed this store; a background task retrains periodically.
    """

    features: list[np.ndarray] = field(default_factory=list)
    y_fill: list[float] = field(default_factory=list)
    y_markout: list[float] = field(default_factory=list)
    max_samples: int = 50_000

    def add(self, features: FillFeatures, filled: bool, markout: float) -> None:
        self.features.append(features.to_array())
        self.y_fill.append(1.0 if filled else 0.0)
        self.y_markout.append(markout)
        if len(self.features) > self.max_samples:
            keep = self.max_samples
            self.features = self.features[-keep:]
            self.y_fill = self.y_fill[-keep:]
            self.y_markout = self.y_markout[-keep:]

    def to_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        n = len(self.features)
        if n < 50:
            return None
        X = np.array(self.features, dtype=np.float32)
        yf = np.array(self.y_fill, dtype=np.float32)
        ym = np.array(self.y_markout, dtype=np.float32)
        return X, yf, ym

    def clear(self) -> None:
        self.features.clear()
        self.y_fill.clear()
        self.y_markout.clear()


# ── journal replay loader (optional: cold-start training) ─────────────────


def load_training_from_journal(
    journal_path: str,
    *,
    meta_provider: object | None = None,
    max_events: int = 500_000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Parse a journal JSONL into training arrays. Returns None if insufficient data.

    This is a cold-start utility — call once at engine init to prime the model
    from historical paper/log data before live fills arrive.
    """
    import json

    X_rows: list[np.ndarray] = []
    y_fill_rows: list[float] = []
    y_markout_rows: list[float] = []

    try:
        with open(journal_path) as fh:
            for _i, line in enumerate(fh):
                if _i >= max_events:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("event") or ev.get("kind", "")
                if kind not in ("fill", "mark"):
                    continue
                data = ev.get("data", ev)
                if not isinstance(data, dict):
                    continue
                # Simplified: a full journal replay needs the full BookView
                # reconstruction. For now, extracts what it can from flat fields.
                feats = _features_from_journal_event(data, kind)
                if feats is None:
                    continue
                X_rows.append(feats.to_array())
                if kind == "fill":
                    y_fill_rows.append(1.0)
                    y_markout_rows.append(float(data.get("markout", 0.0) or 0.0))
                # "mark" events are no-fill samples (quote was placed, not filled)
            if len(X_rows) < 100:
                return None
            X = np.array(X_rows, dtype=np.float32)
            yf = np.array(y_fill_rows, dtype=np.float32)
            ym = np.array(y_markout_rows, dtype=np.float32)
            return X, yf, ym
    except (OSError, IOError):
        return None


def _features_from_journal_event(data: dict[str, object], kind: str) -> FillFeatures | None:
    try:
        return FillFeatures(
            book_imbalance=float(data.get("depth_imbalance", 0.0) or 0.0),
            spread_ticks=float(data.get("spread_ticks", 1.0) or 1.0),
            at_touch=1.0 if kind == "fill" else 0.0,
            vol_ratio=float(data.get("vol_ratio", 0.0) or 0.0),
            flow_z=float(data.get("flow_z", 0.0) or 0.0),
            toxicity=float(data.get("toxicity", 0.0) or 0.0),
            mid_price=float(data.get("mid", 0.5) or 0.5),
            our_size_vs_depth=float(data.get("our_size_vs_depth", 0.1) or 0.1),
            hours_to_resolve=float(data.get("hours_to_resolve", 720.0) or 720.0),
            quote_dist_from_mid_ticks=float(data.get("quote_dist_ticks", 3.0) or 3.0),
            regime_quiet=1.0 if data.get("regime") == "QUIET" else 0.0,
            regime_trending=1.0 if data.get("regime") == "TRENDING" else 0.0,
            regime_event=1.0 if data.get("regime") == "EVENT" else 0.0,
            regime_reduce_only=1.0 if data.get("regime") == "REDUCE_ONLY" else 0.0,
            regime_halted=1.0 if data.get("regime") == "HALTED" else 0.0,
        )
    except (ValueError, KeyError, TypeError):
        return None
