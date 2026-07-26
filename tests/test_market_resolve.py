"""Tests for catalog slug → token resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from polymaker.replay.market_resolve import resolve_market_by_slug


def test_resolve_newsom_from_livecfg_db() -> None:
    db = Path("livecfg/state.db")
    if not db.exists():
        pytest.skip("livecfg/state.db not present")
    meta = resolve_market_by_slug(
        "will-gavin-newsom-win-the-2028-democratic-presidential-nomination-568",
        db_path=db,
    )
    assert meta.yes.token_id.startswith("54533043")
    assert meta.no.token_id.startswith("87854174")
    assert abs(meta.tick_size - 0.001) < 1e-9
    assert meta.rewards_max_spread == pytest.approx(5.5)


def test_resolve_unknown_slug_raises() -> None:
    db = Path("livecfg/state.db")
    if not db.exists():
        pytest.skip("livecfg/state.db not present")
    with pytest.raises(KeyError):
        resolve_market_by_slug("no-such-market-slug-xyz", db_path=db)
