"""Calibration-weighted blending of probability signals.

Problem: naive averaging of unequal sources (book-implied vs external)
overweights the worse-calibrated channel and destroys EV.

Solution: weight each source by how well it has historically been
calibrated. Prefer inverse Brier (or soft reliability weight) so a
source with Brier ≈ 0.25 (coin-flip) gets near-zero influence, while a
well-calibrated source (Brier → 0) dominates.

Pure functions — no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass


_EPS = 1e-12
# Uninformative binary Brier baseline (always predict 0.5).
_UNINFORMATIVE_BRIER = 0.25


@dataclass(frozen=True, slots=True)
class SignalSource:
    """One probability channel with a historical calibration score."""

    name: str
    probability: float
    brier_score: float  # lower is better; 0.25 ≈ uninformative


@dataclass(frozen=True, slots=True)
class BlendResult:
    probability: float
    weights: tuple[tuple[str, float], ...]
    n_sources: int


def calibration_weight(brier: float, *, floor: float = 0.0) -> float:
    """Map a Brier score to a non-negative blend weight.

    weight = max(0, uninformative_brier - brier) / uninformative_brier
    Perfect calibration (brier=0) → weight 1.0
    Uninformative (brier≥0.25) → weight 0.0
    """
    gap = _UNINFORMATIVE_BRIER - float(brier)
    if gap <= 0.0:
        return max(0.0, floor)
    return max(floor, gap / _UNINFORMATIVE_BRIER)


def blend_probabilities(
    sources: list[SignalSource] | tuple[SignalSource, ...],
    *,
    weight_floor: float = 0.0,
) -> BlendResult:
    """Blend probability estimates weighted by historical calibration.

    If every source is uninformative (all weights 0), fall back to an
    equal-weight average so the blend still returns a defined value.
    """
    if not sources:
        return BlendResult(probability=0.5, weights=(), n_sources=0)

    named_weights: list[tuple[str, float]] = []
    for s in sources:
        p = min(max(float(s.probability), 0.0), 1.0)
        w = calibration_weight(s.brier_score, floor=weight_floor)
        named_weights.append((s.name, w))

    total_w = sum(w for _, w in named_weights)
    if total_w <= _EPS:
        # All uninformative → equal weight fallback
        n = len(sources)
        probs = [min(max(float(s.probability), 0.0), 1.0) for s in sources]
        avg = sum(probs) / n
        eq = tuple((s.name, 1.0 / n) for s in sources)
        return BlendResult(probability=round(avg, 8), weights=eq, n_sources=n)

    blended = 0.0
    normed: list[tuple[str, float]] = []
    for s, (_, w) in zip(sources, named_weights):
        p = min(max(float(s.probability), 0.0), 1.0)
        nw = w / total_w
        blended += nw * p
        normed.append((s.name, round(nw, 8)))

    return BlendResult(
        probability=round(blended, 8),
        weights=tuple(normed),
        n_sources=len(sources),
    )
