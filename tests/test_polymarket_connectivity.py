"""Tests for Polymarket connectivity probe (mocked network)."""

from __future__ import annotations

import scripts.polymarket_connectivity as conn


def test_probe_rest_records_failure(monkeypatch) -> None:
    def boom(_url: str, timeout: float = 10):  # noqa: ARG001
        raise TimeoutError("simulated")

    monkeypatch.setattr(conn.urllib.request, "urlopen", boom)
    row = conn._probe_rest("https://example.invalid", 1.0)
    assert row["ok"] is False
    assert "TimeoutError" in row["error"]
