"""LLM-ranked market discovery for Polymaker V3.

Contract:
  1. Pull candidates from Gamma (catalog.scanner.run_scan) or accept a list.
  2. Filter: rewards_min_size <= 200, open-ish (rewards_daily_rate > 0),
     enough liquidity.
  3. Take top 30 by a cheap heuristic (reward rate × liquidity).
  4. Ask Grok to rank via rank_markets tool → structured list.
  5. Cache results for CACHE_TTL_S (default 10 min).

Does not edit markets.toml or activate markets — returns rankings only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from polymaker.intelligence.agent import GrokAgent
from polymaker.intelligence.memory import AgentMemory
from polymaker.intelligence.prompts import RANK_MARKETS_TOOL_SCHEMA, prompt_rank_markets

log = logging.getLogger("polymaker.intelligence.discovery")

MAX_REWARDS_MIN_SIZE = 200.0
TOP_CANDIDATES = 30
CACHE_TTL_S = 600.0
DEFAULT_MIN_LIQUIDITY = 5_000.0


@dataclass
class RankedMarket:
    condition_id: str
    confidence: float
    narrative: str
    suggested_size_pct: float
    risk_notes: str
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiscoveryResult:
    rankings: list[RankedMarket]
    cached: bool
    ts: float
    n_scanned: int = 0
    n_filtered: int = 0
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rankings": [r.as_dict() for r in self.rankings],
            "cached": self.cached,
            "ts": self.ts,
            "n_scanned": self.n_scanned,
            "n_filtered": self.n_filtered,
            "usage": self.usage,
        }


def market_to_candidate(m: Any) -> dict[str, Any]:
    """Normalize MarketMeta or dict into LLM candidate payload."""
    if isinstance(m, dict):
        return {
            "condition_id": str(m.get("condition_id") or m.get("id") or ""),
            "question": str(m.get("question") or m.get("slug") or ""),
            "slug": str(m.get("slug") or ""),
            "rewards_daily_rate": float(m.get("rewards_daily_rate") or 0.0),
            "rewards_min_size": float(m.get("rewards_min_size") or 0.0),
            "rewards_max_spread": float(m.get("rewards_max_spread") or 0.0),
            "liquidity_num": float(m.get("liquidity_num") or m.get("liquidity") or 0.0),
            "volume_24hr": float(m.get("volume_24hr") or 0.0),
            "best_bid": float(m.get("best_bid") or 0.0),
            "best_ask": float(m.get("best_ask") or 0.0),
            "category": str(m.get("category") or ""),
            "edge_estimate": m.get("edge_estimate"),
        }
    return {
        "condition_id": str(getattr(m, "condition_id", "") or ""),
        "question": str(getattr(m, "question", "") or ""),
        "slug": str(getattr(m, "slug", "") or ""),
        "rewards_daily_rate": float(getattr(m, "rewards_daily_rate", 0.0) or 0.0),
        "rewards_min_size": float(getattr(m, "rewards_min_size", 0.0) or 0.0),
        "rewards_max_spread": float(getattr(m, "rewards_max_spread", 0.0) or 0.0),
        "liquidity_num": float(getattr(m, "liquidity_num", 0.0) or 0.0),
        "volume_24hr": float(getattr(m, "volume_24hr", 0.0) or 0.0),
        "best_bid": float(getattr(m, "best_bid", 0.0) or 0.0),
        "best_ask": float(getattr(m, "best_ask", 0.0) or 0.0),
        "category": str(getattr(m, "category", "") or ""),
        "edge_estimate": None,
    }


def filter_candidates(
    markets: Sequence[Any],
    *,
    max_rewards_min_size: float = MAX_REWARDS_MIN_SIZE,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
) -> list[dict[str, Any]]:
    """Filter for reward-eligible, liquid, open-ish markets."""
    out: list[dict[str, Any]] = []
    for m in markets:
        c = market_to_candidate(m)
        if not c["condition_id"]:
            continue
        if c["rewards_daily_rate"] <= 0:
            continue
        if c["rewards_min_size"] > max_rewards_min_size:
            continue
        if c["liquidity_num"] < min_liquidity and c["volume_24hr"] < min_liquidity:
            # allow if either liquidity or 24h volume clears threshold
            if c["liquidity_num"] <= 0 and c["volume_24hr"] <= 0:
                continue
            if c["liquidity_num"] < min_liquidity and c["volume_24hr"] < min_liquidity * 0.1:
                continue
        out.append(c)
    return out


def heuristic_rank(candidates: list[dict[str, Any]], top_n: int = TOP_CANDIDATES) -> list[dict[str, Any]]:
    """Cheap pre-rank before LLM: reward × log-ish liquidity."""
    import math

    def score(c: dict[str, Any]) -> float:
        liq = max(c.get("liquidity_num") or 0.0, c.get("volume_24hr") or 0.0, 1.0)
        return float(c.get("rewards_daily_rate") or 0.0) * math.log10(10.0 + liq)

    return sorted(candidates, key=score, reverse=True)[:top_n]


class MarketDiscovery:
    """Gamma scan + LLM rank with TTL cache."""

    def __init__(
        self,
        agent: GrokAgent,
        memory: AgentMemory | None = None,
        *,
        cache_ttl_s: float = CACHE_TTL_S,
        capital_usdc: float | None = None,
    ) -> None:
        self.agent = agent
        self.memory = memory
        self.cache_ttl_s = float(cache_ttl_s)
        self.capital_usdc = capital_usdc
        self._cache: DiscoveryResult | None = None
        self._llm_calls = 0

    def clear_cache(self) -> None:
        self._cache = None

    @property
    def cache_valid(self) -> bool:
        if self._cache is None:
            return False
        return (time.time() - self._cache.ts) < self.cache_ttl_s

    async def rank_candidates(
        self,
        markets: Sequence[Any],
        *,
        top_n: int = 5,
        force_refresh: bool = False,
    ) -> DiscoveryResult:
        """Filter + pre-rank + LLM rank. Uses cache unless force_refresh."""
        if self.cache_valid and not force_refresh and self._cache is not None:
            cached = DiscoveryResult(
                rankings=list(self._cache.rankings),
                cached=True,
                ts=self._cache.ts,
                n_scanned=self._cache.n_scanned,
                n_filtered=self._cache.n_filtered,
                usage=dict(self._cache.usage),
            )
            return cached

        scanned = list(markets)
        filtered = filter_candidates(scanned)
        shortlist = heuristic_rank(filtered, TOP_CANDIDATES)

        # Attach optional edge estimates if decision framework is available
        for c in shortlist:
            if c.get("edge_estimate") is None:
                c["edge_estimate"] = _optional_edge_hint(c)

        mem_block = ""
        if self.memory is not None:
            mem_block = self.memory.inject_for_prompt(
                query="market selection rewards liquidity spread",
                k=6,
            )

        system, user = prompt_rank_markets(
            shortlist,
            top_n=top_n,
            capital_usdc=self.capital_usdc,
            memory_block=mem_block,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        args, resp = await self.agent.chat_json_tool(
            messages,
            tool_name="rank_markets",
            tool_schema=RANK_MARKETS_TOOL_SCHEMA,
            kind="discovery",
            description="Rank Polymarket candidates for maker rewards",
        )
        self._llm_calls += 1

        by_id = {c["condition_id"]: c for c in shortlist}
        rankings: list[RankedMarket] = []
        for item in args.get("rankings") or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("condition_id") or "")
            if not cid:
                continue
            rankings.append(
                RankedMarket(
                    condition_id=cid,
                    confidence=float(item.get("confidence") or 0.0),
                    narrative=str(item.get("narrative") or ""),
                    suggested_size_pct=float(item.get("suggested_size_pct") or 0.0),
                    risk_notes=str(item.get("risk_notes") or ""),
                    meta=by_id.get(cid, {}),
                )
            )

        if self.memory is not None and resp.content:
            self.memory.parse_and_store_from_text(resp.content)

        result = DiscoveryResult(
            rankings=rankings,
            cached=False,
            ts=time.time(),
            n_scanned=len(scanned),
            n_filtered=len(filtered),
            usage=resp.usage.as_dict(),
        )
        self._cache = result
        return result

    async def discover_from_gamma(
        self,
        store: Any,
        *,
        tag_slugs: tuple[str, ...] = ("politics",),
        min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
        top_n: int = 5,
        force_refresh: bool = False,
    ) -> DiscoveryResult:
        """Run catalog scanner then LLM rank (import-only reuse of scanner)."""
        from polymaker.catalog.scanner import ScanConfig, run_scan

        if self.cache_valid and not force_refresh and self._cache is not None:
            return await self.rank_candidates([], force_refresh=False)

        cfg = ScanConfig(
            tag_slugs=tag_slugs,
            min_liquidity=min_liquidity,
            rewards_only=True,
        )
        markets = await run_scan(store, cfg)
        return await self.rank_candidates(markets, top_n=top_n, force_refresh=True)


def _optional_edge_hint(c: dict[str, Any]) -> float | None:
    """Best-effort edge hint without requiring engine I/O."""
    try:
        bid = float(c.get("best_bid") or 0.0)
        ask = float(c.get("best_ask") or 0.0)
        if bid > 0 and ask > bid:
            mid = 0.5 * (bid + ask)
            spread = ask - bid
            # Crude: half-spread as maker capture estimate in price units
            return round(0.5 * spread / max(mid, 1e-6), 6)
    except (TypeError, ValueError):
        pass
    return None
