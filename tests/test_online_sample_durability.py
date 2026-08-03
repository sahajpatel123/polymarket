"""Online fill samples must survive a restart.

The deployment gate needs ``min_live_validation_samples`` (200) online rows
before the ML model may gate quotes. But the model — and with it the sample
buffer — was only written to disk once ``is_deployable`` was already True, which
itself requires those 200 rows. Below the threshold nothing persisted, so every
restart began at zero and the gate was unreachable by construction.

Samples are now checkpointed to a sidecar on every retrain tick and on shutdown,
independent of deployability, leaving the validated model artifact untouched.
"""

from __future__ import annotations

import asyncio
import pickle

import numpy as np
import pytest

from polymaker.config import Config, PathsConfig, StrategyProfile
from polymaker.domain import MarketMeta, TokenMeta
from polymaker.engine import Engine
from polymaker.strategy.fill_model import FillFeatures
from polymaker.strategy.regime import RegimeMachine

TOK = "yes-token"


def _meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xc", question="q", slug="s",
        tokens=(TokenMeta(TOK, "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01, neg_risk=False, min_order_size=5.0,
        rewards_min_size=10.0, rewards_max_spread=3.0, rewards_daily_rate=50.0,
        maker_fee_bps=0, taker_fee_bps=100, fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z", event_id="e",
    )


def _engine(tmp_path, model_dir: str | None = None) -> Engine:
    md = model_dir or str(tmp_path / "models")
    cfg = Config(paths=PathsConfig(db=str(tmp_path / "s.db"),
                                  journal_dir=str(tmp_path / "j"),
                                  log_dir=str(tmp_path / "l"),
                                  model_dir=md))
    cfg.engine.journal = False
    eng = Engine(cfg, paper=True)
    m = _meta()
    eng.metas[m.condition_id] = m
    eng.profiles[m.condition_id] = StrategyProfile()
    eng.est[m.condition_id] = Engine._make_estimators(eng.profiles[m.condition_id])
    eng.regime_m[m.condition_id] = RegimeMachine()
    eng._dirty[m.condition_id] = asyncio.Event()
    eng._locks[m.condition_id] = asyncio.Lock()
    eng._running = True
    return eng


def _feats(i: int) -> FillFeatures:
    return FillFeatures(
        book_imbalance=0.1 * (i % 5), spread_ticks=1.0, at_touch=1.0,
        vol_ratio=1.0, flow_z=0.0, toxicity=0.0, mid_price=0.5,
        our_size_vs_depth=0.01, hours_to_resolve=100.0,
        quote_dist_from_mid_ticks=1.0, regime_quiet=1.0, regime_trending=0.0,
        regime_event=0.0, regime_reduce_only=0.0, regime_halted=0.0,
    )


def _add_online(eng: Engine, n: int) -> None:
    for i in range(n):
        eng.fill_store.add(_feats(i), filled=True, markout=0.01 * (1 if i % 2 else -1),
                           source="online")


def test_checkpoint_writes_online_samples(tmp_path) -> None:
    eng = _engine(tmp_path)
    _add_online(eng, 25)
    eng._checkpoint_online_samples()
    path = eng._online_store_path
    assert path.exists(), "online samples were not persisted"
    with path.open("rb") as fh:
        blob = pickle.load(fh)
    assert len(blob["features"]) == 25
    assert len(blob["y_fill"]) == 25
    assert len(blob["y_markout"]) == 25
    eng.state.close()


def test_samples_survive_a_restart(tmp_path) -> None:
    """The property that makes the 200-sample gate reachable at all."""
    model_dir = str(tmp_path / "models")
    eng1 = _engine(tmp_path / "a", model_dir=model_dir)
    _add_online(eng1, 40)
    eng1._checkpoint_online_samples()
    eng1.state.close()

    eng2 = _engine(tmp_path / "b", model_dir=model_dir)
    assert eng2.fill_store.online_arrays() is None, "fresh engine starts empty"
    eng2._restore_online_samples()
    online = eng2.fill_store.online_arrays()
    assert online is not None
    assert len(online[0]) == 40, (
        "restart lost the collected samples — the gate can never be reached"
    )
    eng2.state.close()


def test_accumulates_across_multiple_sessions(tmp_path) -> None:
    model_dir = str(tmp_path / "models")
    total = 0
    for session in range(3):
        eng = _engine(tmp_path / f"s{session}", model_dir=model_dir)
        eng._restore_online_samples()
        _add_online(eng, 30)
        eng._checkpoint_online_samples()
        online = eng.fill_store.online_arrays()
        assert online is not None
        total = len(online[0])
        eng.state.close()
    assert total == 90, f"expected 3x30 accumulated, got {total}"


def test_restore_is_idempotent(tmp_path) -> None:
    """Re-running restore must not duplicate rows into a fake sample count."""
    model_dir = str(tmp_path / "models")
    eng = _engine(tmp_path, model_dir=model_dir)
    _add_online(eng, 20)
    eng._checkpoint_online_samples()
    eng._restore_online_samples()
    eng._restore_online_samples()
    online = eng.fill_store.online_arrays()
    assert online is not None
    assert len(online[0]) == 20, (
        f"restore duplicated samples ({len(online[0])}), which would open the "
        "deployment gate on inflated evidence"
    )
    eng.state.close()


def test_checkpoint_is_atomic_and_leaves_no_tmp(tmp_path) -> None:
    eng = _engine(tmp_path)
    _add_online(eng, 10)
    eng._checkpoint_online_samples()
    assert not eng._online_store_path.with_suffix(".tmp").exists()
    eng.state.close()


def test_empty_buffer_writes_nothing(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng._checkpoint_online_samples()
    assert not eng._online_store_path.exists()
    eng.state.close()


def test_offline_samples_are_not_checkpointed(tmp_path) -> None:
    """Only live evidence gates deployment; the offline tape must not inflate it."""
    eng = _engine(tmp_path)
    for i in range(50):
        eng.fill_store.add(_feats(i), filled=True, markout=0.01, source="offline")
    _add_online(eng, 5)
    eng._checkpoint_online_samples()
    with eng._online_store_path.open("rb") as fh:
        blob = pickle.load(fh)
    assert len(blob["features"]) == 5, "offline rows leaked into the online gate"
    eng.state.close()


def test_shutdown_flushes_samples(tmp_path) -> None:
    eng = _engine(tmp_path)
    _add_online(eng, 12)
    assert not eng._online_store_path.exists()
    asyncio.run(eng.shutdown())
    assert eng._online_store_path.exists(), (
        "a clean stop discarded everything gathered since the last retrain tick"
    )


def test_corrupt_checkpoint_does_not_crash_startup(tmp_path) -> None:
    eng = _engine(tmp_path)
    eng._online_store_path.parent.mkdir(parents=True, exist_ok=True)
    eng._online_store_path.write_bytes(b"not a pickle")
    eng._restore_online_samples()      # must not raise
    assert eng.fill_store.online_arrays() is None
    eng.state.close()


def test_restored_samples_are_usable_for_validation(tmp_path) -> None:
    """Round-tripped arrays must keep the shape the gate validates on."""
    model_dir = str(tmp_path / "models")
    eng = _engine(tmp_path, model_dir=model_dir)
    _add_online(eng, 30)
    eng._checkpoint_online_samples()

    eng2 = _engine(tmp_path / "b", model_dir=model_dir)
    eng2._restore_online_samples()
    online = eng2.fill_store.online_arrays()
    assert online is not None
    X, y_fill, y_mk = online
    assert X.shape == (30, 25), f"feature width changed: {X.shape}"
    assert np.isfinite(X).all()
    assert set(np.unique(y_fill)) <= {0.0, 1.0}
    eng.state.close()
    eng2.state.close()


# ── the gate must require real fills, not just rows ──────────────────────


def test_gate_requires_a_minimum_number_of_online_fills() -> None:
    """Row count alone is not evidence of P(fill) skill.

    Non-fill samples dominate the online slice — one is recorded per kept quote.
    An observed run reached 361 online rows containing just 3 fills, which
    satisfied the 200-row gate. An AUC computed on 3 positives is noise, and the
    model would have been promoted to gating real money on it.
    """
    from polymaker.config import ModelConfig

    m = ModelConfig()
    assert m.min_live_fills > 0, "gate accepts rows with no fills at all"
    assert m.min_live_fills <= m.min_live_validation_samples


def test_engine_gate_counts_fills_not_rows(tmp_path) -> None:
    """361 rows / 3 fills must NOT satisfy the gate."""
    from polymaker.config import ModelConfig

    m = ModelConfig()
    rows, fills = 361, 3
    satisfied = (rows >= m.min_live_validation_samples
                 and fills >= m.min_live_fills)
    assert not satisfied, (
        f"{rows} rows with only {fills} fills opened the deployment gate"
    )
    # and a genuinely evidenced slice does satisfy it
    assert (400 >= m.min_live_validation_samples
            and 60 >= m.min_live_fills)


def test_both_gate_sites_check_fills() -> None:
    """The retrain path and the load path must agree."""
    import inspect

    from polymaker.engine import Engine

    for fn in (Engine._retrain_fill_model, Engine._load_persisted_fill_model):
        src = inspect.getsource(fn)
        if "min_live_validation_samples" not in src:
            continue
        assert "min_live_fills" in src, (
            f"{fn.__name__} gates on row count without requiring fills"
        )
