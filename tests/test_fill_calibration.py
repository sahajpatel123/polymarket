"""Fill calibration table schema (fixture-first)."""

from __future__ import annotations

from polymaker.paper.fill_calibration import FillCalibrationTable


def test_default_empty_schema() -> None:
    t = FillCalibrationTable.default_empty()
    assert t.source == "empty"
    assert len(t.bins) == 4
    assert t.is_calibrated() is False
    d = t.as_dict()
    assert "bins" in d and d["n_live_samples"] == 0


def test_record_updates_bins() -> None:
    t = FillCalibrationTable.default_empty()
    for _ in range(10):
        t.record(0.05, filled=False)
    for _ in range(10):
        t.record(0.05, filled=True)
    assert t.bins[0].n_predicted == 20
    assert abs(t.bins[0].actual_rate - 0.5) < 1e-9
    assert t.source == "tiny_live"
