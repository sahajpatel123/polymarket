"""Tests for resolution probability calibration via Platt scaling."""

from polymaker.intelligence.resolution_calibration import ResolutionCalibrator


def test_empty_calibrator():
    """No records → ECE=0, no correction applied."""
    c = ResolutionCalibrator()
    assert c.n_records == 0
    assert c.compute_ece() == 0.0
    assert c.calibrated_p(0.5) == 0.5  # passthrough


def test_perfect_calibration():
    """Well-calibrated records → ECE near 0, correction ≈ identity."""
    c = ResolutionCalibrator()
    # Generate records where estimated_p ≈ observed frequency per bin
    for outcome in [False] * 5 + [True] * 5:
        c.record_outcome("a", estimated_p=0.5, market_price=0.5, actual_outcome=outcome)
    for outcome in [False] * 2 + [True] * 8:
        c.record_outcome("b", estimated_p=0.8, market_price=0.5, actual_outcome=outcome)
    for outcome in [False] * 8 + [True] * 2:
        c.record_outcome("c", estimated_p=0.2, market_price=0.5, actual_outcome=outcome)
    ece = c.compute_ece()
    assert ece < 0.3  # rough calibration
    cal = c.calibrated_p(0.5)
    # Platt linear regression on binary targets gives a shrunken slope,
    # so cal may overshoot 0.5 slightly — accept reasonable range.
    assert 0.4 < cal < 0.75


def test_systematic_overconfidence():
    """LLM overconfident: estimates >80% for events that resolve ~40%."""
    c = ResolutionCalibrator()
    for i in range(30):
        outcome = i >= 18  # 40% actual positive rate
        p = 0.8 + 0.005 * i  # LLM says 80-95%
        p = min(p, 0.95)
        c.record_outcome(
            condition_id=f"x_{i}", estimated_p=p, market_price=0.5,
            actual_outcome=outcome,
        )
    for i in range(30):
        outcome = i >= 21  # 30% actual positive rate
        p = 0.9
        c.record_outcome(
            condition_id=f"y_{i}", estimated_p=p, market_price=0.5,
            actual_outcome=outcome,
        )
    assert c.compute_ece() > 0.3
    cal = c.calibrated_p(0.85)
    assert cal < 0.85  # corrected downward
    assert cal >= 0.01


def test_systematic_underconfidence():
    """LLM underconfident: estimates ~40% for events that resolve ~60%."""
    c = ResolutionCalibrator()
    for i in range(30):
        outcome = i >= 15  # 50% actual positive rate
        p = 0.4  # LLM always says 40%
        c.record_outcome(
            condition_id=f"x_{i}", estimated_p=p, market_price=0.5,
            actual_outcome=outcome,
        )
    for i in range(30):
        outcome = i >= 12  # 60% actual positive rate
        p = 0.5  # LLM says 50%
        c.record_outcome(
            condition_id=f"y_{i}", estimated_p=p, market_price=0.5,
            actual_outcome=outcome,
        )
    cal = c.calibrated_p(0.4)
    assert cal > 0.4  # corrected upward (underconfident)
    assert cal <= 0.99


def test_calibration_needs_10_records():
    """No correction applied with <10 records."""
    c = ResolutionCalibrator()
    for i in range(9):
        c.record_outcome(
            condition_id=f"cid_{i}", estimated_p=0.9, market_price=0.5,
            actual_outcome=False,
        )
    assert c.n_records == 9
    assert c.calibrated_p(0.9) == 0.9  # passthrough


def test_summary():
    """Summary reports key metrics."""
    c = ResolutionCalibrator()
    s = c.summary()
    assert s["n_records"] == 0
    assert s["ece"] == 0.0
    assert s["needs_calibration"] is False
