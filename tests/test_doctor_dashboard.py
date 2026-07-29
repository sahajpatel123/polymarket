"""Doctor reports live-dashboard bind readiness."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rich.console import Console

from polymaker import doctor as doc


@pytest.mark.asyncio
async def test_doctor_dashboard_bind_check(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(
        profiles={"p": object()},
        markets=[],
        secrets=SimpleNamespace(has_wallet=False, browser_address=""),
        wallet=SimpleNamespace(
            signature_type=0,
            clob_host="https://example.invalid",
            gamma_host="https://example.invalid",
        ),
        proxy=None,
        engine=SimpleNamespace(
            dashboard_host="127.0.0.1",
            dashboard_port=8765,
            dashboard_enabled=True,
        ),
        paths=SimpleNamespace(log_dir=str(tmp_path)),
    )

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise doc.httpx.HTTPError("skip")

    async def _no_token(_cfg):
        return None

    monkeypatch.setattr(doc.httpx, "AsyncClient", lambda **k: _Boom())
    monkeypatch.setattr(doc, "_top_political_token", _no_token)

    console = Console(record=True, width=120)
    await doc.run_doctor(cfg, console)  # type: ignore[arg-type]
    text = console.export_text()
    assert "dashboard bind" in text.lower()
    assert "127.0.0.1:8765" in text


@pytest.mark.asyncio
async def test_doctor_dashboard_disabled_still_passes_bind(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = SimpleNamespace(
        profiles={"p": object()},
        markets=[],
        secrets=SimpleNamespace(has_wallet=False, browser_address=""),
        wallet=SimpleNamespace(
            signature_type=0,
            clob_host="https://example.invalid",
            gamma_host="https://example.invalid",
        ),
        proxy=None,
        engine=SimpleNamespace(
            dashboard_host="127.0.0.1",
            dashboard_port=8765,
            dashboard_enabled=False,
        ),
        paths=SimpleNamespace(log_dir=str(tmp_path)),
    )

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise doc.httpx.HTTPError("skip")

    async def _no_token(_cfg):
        return None

    monkeypatch.setattr(doc.httpx, "AsyncClient", lambda **k: _Boom())
    monkeypatch.setattr(doc, "_top_political_token", _no_token)

    console = Console(record=True, width=120)
    await doc.run_doctor(cfg, console)  # type: ignore[arg-type]
    text = console.export_text()
    assert "dashboard bind" in text.lower()
    assert "disabled" in text.lower()
    # The dashboard line itself should be a pass mark (✓), not fail READY alone.
    assert "✓ dashboard bind" in text or "dashboard bind (loopback)" in text
