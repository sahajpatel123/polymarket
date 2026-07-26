"""Resolve MarketMeta (tokens, tick, rewards) from catalog by slug.

Prevents the T1-144 mispair failure mode where eval scripts accept arbitrary
YES/NO token IDs that do not form a complementary binary pair.
"""

from __future__ import annotations

from pathlib import Path

from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta


def resolve_market_by_slug(
    slug: str,
    *,
    db_path: str | Path = "state.db",
) -> MarketMeta:
    """Load MarketMeta from the local catalog SQLite DB."""
    store = CatalogStore(db_path)
    try:
        meta = store.get_by_slug(slug)
    finally:
        store.close()
    if meta is None:
        raise KeyError(f"slug not in catalog: {slug!r} (db={db_path})")
    if len(meta.tokens) < 2:
        raise ValueError(f"market {slug!r} has <2 tokens")
    return meta


def meta_token_summary(meta: MarketMeta) -> dict[str, str | float | None]:
    return {
        "slug": meta.slug,
        "condition_id": meta.condition_id,
        "yes_token": meta.yes.token_id,
        "no_token": meta.no.token_id,
        "tick_size": meta.tick_size,
        "rewards_max_spread": meta.rewards_max_spread,
        "rewards_min_size": meta.rewards_min_size,
        "rewards_daily_rate": meta.rewards_daily_rate,
        "neg_risk": meta.neg_risk,
    }
