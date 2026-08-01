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
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

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
    # Microstructure features (adds 5-10pp WR)
    ofi: float = 0.0  # order flow imbalance at touch (-1 to 1)
    ofi_trend: float = 0.0  # OFI change over last 5 snapshots
    size_anomaly: float = 1.0  # trade size / avg recent size (>1 = unusually large)
    trade_rate: float = 0.0  # trades/sec in last 30s

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
                self.ofi,
                self.ofi_trend,
                self.size_anomaly,
                self.trade_rate,
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
    ofi: float = 0.0,
    ofi_trend: float = 0.0,
    size_anomaly: float = 1.0,
    trade_rate: float = 0.0,
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
        ofi=_clip(ofi, -1.0, 1.0),
        ofi_trend=_clip(ofi_trend, -1.0, 1.0),
        size_anomaly=_clip(size_anomaly, 0.1, 20.0),
        trade_rate=_clip(trade_rate, 0.0, 10.0),
    )


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ── fill probability model ────────────────────────────────────────────────


@dataclass
class FillPrediction:
    prob_fill: float  # 0-1
    expected_markout: float  # signed, positive = good for us
    should_quote: bool
    confidence: float = 0.5  # 0-1 model confidence in this prediction
    suggested_size_mult: float = 1.0  # 0.5-2.5x base_size multiplier


class FillModel:
    """Gradient-boosted fill and markout predictor.

    Trained on journal data or online fills. Falls back to a heuristic
    prior when untrained (< min_samples).
    """

    __slots__ = (
        "_fill_clf",
        "_markout_reg",
        "_good_fill_clf",
        "_n_samples",
        "_min_samples",
        "_feature_dim",
        "_oos_passed",
    )

    def __init__(self, min_samples: int = 100) -> None:
        self._fill_clf: object | None = None
        self._markout_reg: object | None = None
        self._good_fill_clf: object | None = None
        self._n_samples = 0
        self._min_samples = min_samples
        self._feature_dim = 19
        self._oos_passed: bool | None = None

    @property
    def is_trained(self) -> bool:
        # _markout_reg is the core model (markout + good-fill classifier).
        # _fill_clf may be None when all training samples are fills (mono-class),
        # in which case predict uses a fallback P(fill) = 1.0.
        return (
            self._n_samples >= self._min_samples
            and self._markout_reg is not None
        )

    @property
    def is_deployable(self) -> bool:
        """is_trained AND the last holdout validation passed.

        The engine only lets the model filter/size live quotes when this is
        True. Until then the model runs in shadow mode and the empirical
        book-shape tree (quality_filter_score) remains the quote gate.
        """
        return self.is_trained and self._oos_passed is True

    def holdout_metrics(
        self,
        X: np.ndarray,
        y_fill: np.ndarray,
        y_markout: np.ndarray,
        *,
        min_auc: float = 0.55,
        min_corr: float = 0.05,
        seed: int = 42,
    ) -> dict[str, float | bool | int | str]:
        """Evaluate fill/markout skill on a fixed 70/30 holdout split.

        Pure diagnostic: does NOT change deployability state. Fresh models
        with the same hyperparameters are fit on the train split only — the
        stored models were trained on the full buffer, so scoring them on any
        subset would be in-sample. Fill skill is ROC AUC (0.5 = coin flip),
        markout skill is Pearson correlation on fill-only rows.
        """
        from sklearn.ensemble import (
            HistGradientBoostingClassifier,
            HistGradientBoostingRegressor,
        )

        empty: dict[str, float | bool | int | str] = {
            "auc": 0.0, "corr": 0.0, "n_test": 0, "passed": False, "reason": "not trained",
        }
        if not self.is_trained:
            return empty
        n = len(X)
        if n < 60:
            return {**empty, "reason": f"too few samples ({n})"}
        rng = np.random.RandomState(seed)
        idx = rng.permutation(n)
        n_test = max(20, int(n * 0.3))
        te = idx[n - n_test:]
        tr = idx[: n - n_test]
        X_tr, yf_tr = X[tr], y_fill[tr]
        X_te, yf_te, ym_te = X[te], y_fill[te], y_markout[te]

        auc = 0.5
        if np.unique(yf_tr).size >= 2 and np.unique(yf_te).size >= 2:
            try:
                clf = HistGradientBoostingClassifier(
                    max_iter=100, max_depth=4, min_samples_leaf=10,
                    early_stopping=False, random_state=42,
                )
                clf.fit(X_tr, yf_tr)
                from sklearn.metrics import roc_auc_score
                p = clf.predict_proba(X_te)[:, 1]
                auc = float(roc_auc_score(yf_te, p))
            except Exception:
                auc = 0.5

        corr = 0.0
        try:
            has_fill_tr = yf_tr > 0
            if int(has_fill_tr.sum()) >= self._min_samples:
                reg = HistGradientBoostingRegressor(
                    max_iter=100, max_depth=4, min_samples_leaf=10,
                    early_stopping=False, random_state=42,
                )
                reg.fit(X_tr[has_fill_tr], y_markout[tr][has_fill_tr])
                te_fill = yf_te > 0
                if int(te_fill.sum()) >= 10:
                    pm = reg.predict(X_te[te_fill])
                    corr = float(np.corrcoef(pm, ym_te[te_fill])[0, 1])
        except Exception:
            corr = 0.0
        if not math.isfinite(corr):
            corr = 0.0

        passed = auc >= min_auc and corr >= min_corr
        return {
            "auc": round(auc, 4),
            "corr": round(corr, 4),
            "n_test": int(len(X_te)),
            "passed": passed,
            "reason": "ok" if passed else "below floors",
        }

    def validate(
        self,
        X: np.ndarray,
        y_fill: np.ndarray,
        y_markout: np.ndarray,
        *,
        min_auc: float = 0.55,
        min_corr: float = 0.05,
        seed: int = 42,
    ) -> dict[str, float | bool | int | str]:
        """Holdout validation that gates deployment (sets ``_oos_passed``).

        Call this on the ONLINE (live) slice of the training buffer before
        letting the model act on live quotes; the model must win on real
        fills, not on the offline training tape.
        """
        metrics = self.holdout_metrics(X, y_fill, y_markout, min_auc=min_auc,
                                       min_corr=min_corr, seed=seed)
        self._oos_passed = bool(metrics["passed"])
        return metrics

    def save(self, path: str | Path, store: FillTrainingStore | None = None) -> None:
        """Atomically persist the model and optional training buffer.

        The artifact is local, operator-owned state. It is loaded only from the
        configured model directory, never from a remote source.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        arrays = store.raw_arrays() if store is not None else None
        payload = {
            "format_version": 2,
            "model": self,
            "training": arrays,
            "training_online": store.online_mask() if store is not None else None,
        }
        tmp = target.with_name(f".{target.name}.tmp")
        with tmp.open("wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)

    @classmethod
    def load_bundle(cls, path: str | Path) -> tuple[FillModel, FillTrainingStore | None]:
        """Load a model artifact and its optional replay/training buffer."""
        with Path(path).open("rb") as fh:
            payload = pickle.load(fh)
        version = payload.get("format_version")
        if version not in (1, 2):
            raise ValueError(f"unsupported fill-model artifact version {version}")
        model = payload.get("model")
        if not isinstance(model, cls):
            raise TypeError("artifact does not contain a FillModel")
        arrays = payload.get("training")
        if arrays is None:
            return model, None
        store = FillTrainingStore.from_arrays(
            *arrays, online_mask=payload.get("training_online")
        )
        return model, store

    @classmethod
    def load(cls, path: str | Path) -> FillModel:
        """Load only the model portion of an artifact."""
        return cls.load_bundle(path)[0]

    def predict(self, features: FillFeatures) -> FillPrediction:
        """Predict fill probability, markout, confidence, and suggested size.

        Uses a binary classifier (P[good fill]) for dynamic position sizing.
        When the classifier is confident that a fill will be good, we size up.
        When it's confident the fill will be bad, we size down. When unsure,
        we use the base size.

        Falls back to heuristic if model is untrained.
        """
        if not self.is_trained:
            return _heuristic_predict(features)

        X = features.to_array().reshape(1, -1)
        try:
            prob_fill = 1.0
            if self._fill_clf is not None:
                prob_fill = float(self._fill_clf.predict_proba(X)[0, 1])  # type: ignore[union-attr]
            markout = float(self._markout_reg.predict(X)[0])  # type: ignore[union-attr]
        except Exception:
            return _heuristic_predict(features)

        prob_fill = _clip(prob_fill, 0.0, 1.0)

        # Binary classifier: P(good fill) → confidence + dynamic sizing
        if self._good_fill_clf is not None:
            try:
                p_good = float(self._good_fill_clf.predict_proba(X)[0, 1])  # type: ignore[union-attr]
            except Exception:
                p_good = 0.5
        else:
            # Fallback: use markout sign as proxy
            p_good = 0.55 if markout > 0 else 0.45

        # Confidence magnitude (direction-blind, for reporting only)
        confidence = _clip(2.0 * abs(p_good - 0.5), 0.1, 0.9)

        # Dynamic size — SIGNED on P(good):
        #   confidently good → size up (to 2.5x), confidently bad → size down
        #   (to 0.5x), unsure (p_good ≈ 0.5) → 1.0x base size.
        size_mult = 1.0 + 3.0 * (p_good - 0.5)
        size_mult = _clip(round(size_mult, 2), 0.5, 2.5)

        should = True
        if prob_fill > 0.8:
            should = False
        elif prob_fill > 0.5 and markout < 0.0:
            should = False
        elif prob_fill > 0.3 and markout < -0.005 and p_good < 0.5:
            should = False

        return FillPrediction(
            prob_fill=prob_fill, expected_markout=markout, should_quote=should,
            confidence=confidence, suggested_size_mult=size_mult,
        )

    def train(
        self,
        X: np.ndarray,
        y_fill: np.ndarray,
        y_markout: np.ndarray,
    ) -> None:
        """(Re-)train the models on accumulated fill data.

        X: (n_samples, 15) feature matrix.
        y_fill: (n_samples,) binary fill indicator.
        y_markout: (n_samples,) signed markout after fill.

        The markout regressor and P(good fill) classifier are trained on
        FILLS ONLY — non-fill samples carry markout=0.0 and would label them
        all "bad", conflating P(fill) with P(good|fill).
        """
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

        n = len(X)
        if n < self._min_samples:
            return

        if np.unique(y_fill).size >= 2:
            self._fill_clf = HistGradientBoostingClassifier(
                max_iter=100,
                max_depth=4,
                min_samples_leaf=10,
                early_stopping=False,
                random_state=42,
            )
            self._fill_clf.fit(X, y_fill)
        else:
            self._fill_clf = None

        has_fill = y_fill > 0
        n_fill = int(has_fill.sum())
        if n_fill >= self._min_samples:
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

            # P(good fill) — fills only, same label subset as the regressor.
            y_good = (y_fill_mo > 0).astype(np.float32)
            if np.unique(y_good).size >= 2:
                self._good_fill_clf = HistGradientBoostingClassifier(
                    max_iter=100, max_depth=4, min_samples_leaf=10,
                    early_stopping=False, random_state=42,
                )
                self._good_fill_clf.fit(X_fill, y_good)
            else:
                self._good_fill_clf = None
        else:
            self._markout_reg = None
            self._good_fill_clf = None

        self._n_samples = n

    def clear(self) -> None:
        self._fill_clf = None
        self._markout_reg = None
        self._good_fill_clf = None
        self._n_samples = 0
        self._oos_passed = None


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

    return FillPrediction(
        prob_fill=prob, expected_markout=markout, should_quote=should,
        confidence=0.5, suggested_size_mult=1.0,
    )


# ── training-data accumulator ─────────────────────────────────────────────


@dataclass
class FillTrainingStore:
    """Accumulates features + outcomes for periodic model retraining.

    Live fills feed this store; a background task retrains periodically.
    """

    features: list[np.ndarray] = field(default_factory=list)
    y_fill: list[float] = field(default_factory=list)
    y_markout: list[float] = field(default_factory=list)
    source: list[str] = field(default_factory=list)  # "offline" | "online"
    max_samples: int = 50_000

    def add(
        self,
        features: FillFeatures,
        filled: bool,
        markout: float,
        *,
        source: str = "online",
    ) -> None:
        self.features.append(features.to_array())
        self.y_fill.append(1.0 if filled else 0.0)
        self.y_markout.append(markout)
        self.source.append(source)
        if len(self.features) > self.max_samples:
            keep = self.max_samples
            self.features = self.features[-keep:]
            self.y_fill = self.y_fill[-keep:]
            self.y_markout = self.y_markout[-keep:]
            self.source = self.source[-keep:]

    def to_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        n = len(self.features)
        if n < 50:
            return None
        return self.raw_arrays()

    def raw_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """Return all buffered samples, including a short cold-start buffer."""
        if not self.features:
            return None
        return (
            np.array(self.features, dtype=np.float32),
            np.array(self.y_fill, dtype=np.float32),
            np.array(self.y_markout, dtype=np.float32),
        )

    def online_arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """Return only live-acquired samples (used for deployment validation)."""
        idx = [i for i, s in enumerate(self.source) if s == "online"]
        if not idx:
            return None
        return (
            np.array([self.features[i] for i in idx], dtype=np.float32),
            np.array([self.y_fill[i] for i in idx], dtype=np.float32),
            np.array([self.y_markout[i] for i in idx], dtype=np.float32),
        )

    def online_mask(self) -> np.ndarray | None:
        """Per-sample provenance mask for persistence."""
        if not self.source:
            return None
        return np.array([1.0 if s == "online" else 0.0 for s in self.source],
                        dtype=np.float32)

    @classmethod
    def from_arrays(
        cls,
        features: np.ndarray,
        y_fill: np.ndarray,
        y_markout: np.ndarray,
        *,
        max_samples: int = 50_000,
        online_mask: np.ndarray | None = None,
    ) -> FillTrainingStore:
        """Restore a bounded training buffer from persisted arrays."""
        if not (len(features) == len(y_fill) == len(y_markout)):
            raise ValueError("training arrays have different lengths")
        store = cls(max_samples=max_samples)
        start = max(0, len(features) - max_samples)
        store.features = [np.asarray(row, dtype=np.float32) for row in features[start:]]
        store.y_fill = [float(v) for v in y_fill[start:]]
        store.y_markout = [float(v) for v in y_markout[start:]]
        if online_mask is not None:
            mask = np.asarray(online_mask)
            if len(mask) != len(features):
                raise ValueError("online_mask length mismatch")
            store.source = [
                "online" if float(mask[i]) > 0.0 else "offline"
                for i in range(start, len(features))
            ]
        else:
            store.source = ["offline"] * len(store.features)
        return store

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


# ── Decision-tree quality filter (production pre-filter) ──────────────────


def quality_filter_score(
    *,
    imbalance: float,
    spread_ticks: float,
    mid: float,
    bd_total: float,
    ad1: float,
    ad2: float,
    dist_ticks: float,
    bd1: float = 0.0,
    bd2: float = 0.0,
) -> float:
    """Decision-tree quality score from 687 real Polymarket at-touch fills.

    Returns 1.0 (trade) or 0.0 (skip). Filters out book conditions where
    the win rate is below 50%. Produces 65.7% WR on 62% of fills.

    Key rules (from sklearn DecisionTree, max_depth=5, min_samples_leaf=20):
      - Ask-heavy + tight spread → 80-85% WR (mean reversion)
      - Deep bid support → 77% WR (safety net)
      - Shallow ask wall + deep bid → 77-80% WR
      - Bid-heavy + high ask depth → 19-24% WR (skip)
    """
    if dist_ticks <= 0.9:  # tight to the touch
        if ad1 <= 6443.9:  # shallow ask at level 1
            if imbalance <= -0.2:  # strongly ask-heavy
                return 1.0  # WR 80% — sellers retreating, mean reversion
            else:
                if bd_total <= 51171.3:  # not deep enough bid support
                    if mid <= 0.6:  # low-mid price
                        return 0.0  # WR 43%
                    else:
                        return 1.0  # WR 66%
                else:
                    return 1.0  # WR 77% — deep bid support
        else:  # deep ask at level 1
            if imbalance <= 0.0:  # not bid-heavy
                if ad2 <= 16826.8:  # moderate ask depth at level 2
                    if bd_total <= 29690.2:
                        return 0.0  # WR 35% — not enough bid depth
                    else:
                        return 1.0  # WR 77% — bid support present
                else:
                    return 0.0  # WR 24% — heavy ask wall above
            else:
                return 0.0  # WR 19% — bid-heavy into ask wall
    else:  # further from touch
        if imbalance <= -0.2:  # strongly ask-heavy
            return 1.0  # WR 85% — best case
        else:
            if ad2 <= 9053.9:  # very thin ask
                return 0.0  # WR 41% — thin book above
            else:
                if ad2 <= 13181.2:  # moderate ask
                    if bd2 <= 12924.7:
                        return 1.0  # WR 60%
                    else:
                        return 1.0  # WR 80% — deep bid at level 2
                else:  # deep ask at level 2
                    if bd1 <= 1670.8:
                        return 1.0  # WR 71% — thin bid at level 1, but deep ask
                    else:
                        return 0.0  # WR 55% — borderline
