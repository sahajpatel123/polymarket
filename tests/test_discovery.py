"""LLM discovery ranking + cache (mock agent)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from polymaker.intelligence.agent import AgentResponse, TokenUsage, ToolCall
from polymaker.intelligence.discovery import (
    MarketDiscovery,
    filter_candidates,
    heuristic_rank,
    market_to_candidate,
)


@dataclass
class FakeAgent:
    rankings: list[dict[str, Any]]
    calls: int = 0

    async def chat_json_tool(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        payload = {"rankings": self.rankings}
        resp = AgentResponse(
            content="rank ok",
            tool_calls=[ToolCall(id="1", name="rank_markets", arguments=payload)],
            usage=TokenUsage(total_tokens=20, prompt_tokens=10, completion_tokens=10),
        )
        return payload, resp


def _mk(
    cid: str,
    *,
    rate: float = 50.0,
    min_size: float = 50.0,
    liq: float = 20_000.0,
) -> dict[str, Any]:
    return {
        "condition_id": cid,
        "question": f"Q {cid}",
        "slug": cid,
        "rewards_daily_rate": rate,
        "rewards_min_size": min_size,
        "rewards_max_spread": 3.0,
        "liquidity_num": liq,
        "volume_24hr": liq * 0.5,
        "best_bid": 0.48,
        "best_ask": 0.52,
        "category": "politics",
    }


def test_filter_and_heuristic() -> None:
    markets = [
        _mk("good", min_size=50, rate=100, liq=50_000),
        _mk("too_big_min", min_size=500, rate=200, liq=100_000),
        _mk("no_reward", rate=0.0, min_size=20, liq=50_000),
        _mk("ok2", min_size=100, rate=40, liq=15_000),
    ]
    filtered = filter_candidates(markets, max_rewards_min_size=200)
    ids = {c["condition_id"] for c in filtered}
    assert "good" in ids
    assert "ok2" in ids
    assert "too_big_min" not in ids
    assert "no_reward" not in ids
    top = heuristic_rank(filtered, 1)
    assert top[0]["condition_id"] == "good"


@pytest.mark.asyncio
async def test_rank_and_cache() -> None:
    agent = FakeAgent(
        rankings=[
            {
                "condition_id": "good",
                "confidence": 0.9,
                "narrative": "dense rewards",
                "suggested_size_pct": 0.05,
                "risk_notes": "ok",
            }
        ]
    )
    disc = MarketDiscovery(agent, cache_ttl_s=600)  # type: ignore[arg-type]
    markets = [_mk("good"), _mk("ok2")]
    r1 = await disc.rank_candidates(markets, top_n=3)
    assert r1.cached is False
    assert agent.calls == 1
    assert r1.rankings[0].condition_id == "good"
    assert r1.rankings[0].confidence == 0.9
    r2 = await disc.rank_candidates(markets, top_n=3)
    assert r2.cached is True
    assert agent.calls == 1  # no second HTTP
    r3 = await disc.rank_candidates(markets, top_n=3, force_refresh=True)
    assert r3.cached is False
    assert agent.calls == 2


def test_market_to_candidate_object() -> None:
    class M:
        condition_id = "c1"
        question = "Q"
        slug = "s"
        rewards_daily_rate = 10.0
        rewards_min_size = 20.0
        rewards_max_spread = 3.0
        liquidity_num = 1000.0
        volume_24hr = 500.0
        best_bid = 0.4
        best_ask = 0.6
        category = "sports"

    c = market_to_candidate(M())
    assert c["condition_id"] == "c1"
    assert c["rewards_min_size"] == 20.0
