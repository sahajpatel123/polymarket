"""Tests for the auto-discovery engine config and MarketDataService changes."""

from __future__ import annotations


def test_engine_config_has_auto_discovery_defaults() -> None:
    """Default engine config should include auto-discovery knobs."""
    from polymaker.config import EngineConfig

    cfg = EngineConfig()
    assert hasattr(cfg, "auto_discovery_enabled")
    assert cfg.auto_discovery_enabled is False
    assert cfg.auto_discovery_interval_s == 3600.0
    assert cfg.auto_discovery_tags == ("politics",)
    assert cfg.auto_discovery_min_score == 0.01
    assert cfg.auto_discovery_max_markets == 20
    assert cfg.auto_discovery_profile == "political-longdated"
    assert cfg.auto_discovery_hot_reload is True


def test_engine_config_auto_discovery_can_be_overridden() -> None:
    """Auto-discovery settings should be overridable via TOML."""
    from polymaker.config import EngineConfig

    cfg = EngineConfig(
        auto_discovery_enabled=True,
        auto_discovery_interval_s=600.0,
        auto_discovery_tags=("politics", "sports", "crypto"),
        auto_discovery_min_score=0.05,
        auto_discovery_max_markets=10,
        auto_discovery_profile="newsom-mm",
        auto_discovery_hot_reload=False,
    )
    assert cfg.auto_discovery_enabled is True
    assert cfg.auto_discovery_interval_s == 600.0
    assert "sports" in cfg.auto_discovery_tags
    assert "crypto" in cfg.auto_discovery_tags
    assert cfg.auto_discovery_min_score == 0.05
    assert cfg.auto_discovery_max_markets == 10
    assert cfg.auto_discovery_profile == "newsom-mm"
    assert cfg.auto_discovery_hot_reload is False


def test_market_data_service_add_market() -> None:
    """Adding a market should register tokens and update subs."""
    from polymaker.marketdata.service import MarketDataService

    svc = MarketDataService()
    svc.set_markets([])
    initial_subs = list(svc._subs)
    assert initial_subs == []

    svc.add_market("cond1", ["tokenA", "tokenB"])
    assert "tokenA" in svc._subs
    assert "tokenB" in svc._subs
    assert svc._token_condition["tokenA"] == "cond1"
    assert svc._token_condition["tokenB"] == "cond1"
    assert "tokenA" in svc.books
    assert "tokenB" in svc.books

    # Adding same market again should be idempotent
    svc.add_market("cond1", ["tokenA", "tokenB"])
    assert svc._subs.count("tokenA") == 1
    assert svc._subs.count("tokenB") == 1


def test_market_data_service_remove_market() -> None:
    """Removing a market should drop its tokens from the subs."""
    from polymaker.marketdata.service import MarketDataService

    svc = MarketDataService()
    svc.set_markets([])
    svc.add_market("cond1", ["tokenA", "tokenB"])
    svc.add_market("cond2", ["tokenC"])
    assert len(svc._subs) == 3

    svc.remove_market("cond1")
    assert "tokenA" not in svc._subs
    assert "tokenB" not in svc._subs
    assert "tokenC" in svc._subs
    assert "tokenA" not in svc._token_condition

    # Removing unknown market should be no-op
    svc.remove_market("unknown")
    assert "tokenC" in svc._subs
