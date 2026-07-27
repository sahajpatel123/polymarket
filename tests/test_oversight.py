"""Oversight loop: action queue + dry_run (mock agent)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from polymaker.intelligence.agent import AgentResponse, TokenUsage, ToolCall
from polymaker.intelligence.memory import AgentMemory
from polymaker.intelligence.oversight import (
    OversightAction,
    OversightLoop,
    apply_actions_via_engine,
    gather_default_snapshot,
)


@dataclass
class FakeAgent:
    payload: dict[str, Any]
    calls: int = 0

    async def chat_json_tool(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        resp = AgentResponse(
            content="commentary",
            tool_calls=[
                ToolCall(id="1", name="oversight_report", arguments=self.payload)
            ],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        return self.payload, resp


@pytest.mark.asyncio
async def test_run_once_queues_actions(tmp_path) -> None:  # noqa: ANN001
    payload = {
        "narrative": "Inventory building on market A",
        "reasoning": "fill rate up",
        "actions": [
            {
                "type": "widen_spread",
                "market": "cidA",
                "params": {"mult": 1.5},
                "dry_run": True,
                "reason": "test",
            },
            {
                "type": "no_op",
                "dry_run": False,
                "reason": "nothing",
            },
        ],
    }
    agent = FakeAgent(payload)
    mem = AgentMemory(tmp_path / "m.db")
    try:
        loop = OversightLoop(agent, mem, interval_s=3600)  # type: ignore[arg-type]
        snap = gather_default_snapshot(pnl=-1.2, drawdown=0.05, fill_rate=0.1)
        report = await loop.run_once(snap)
        assert report.narrative.startswith("Inventory")
        assert len(report.actions) == 2
        q = loop.peek_actions()
        assert len(q) == 2
        assert q[0].type == "widen_spread"
        assert q[0].dry_run is True
        assert agent.calls == 1
        drained = loop.drain_actions()
        assert len(drained) == 2
        assert loop.peek_actions() == []
    finally:
        mem.close()


@pytest.mark.asyncio
async def test_dry_run_does_not_call_engine_mutator() -> None:
    class Engine:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def apply_oversight_action(self, action: OversightAction) -> str:
            self.calls.append(action)
            return "applied"

    eng = Engine()
    actions = [
        OversightAction(type="pause_market", market="m", dry_run=True, reason="x"),
        OversightAction(type="tighten_spread", market="m", dry_run=False, reason="y"),
    ]
    results = await apply_actions_via_engine(eng, actions)
    assert results[0]["status"] == "dry_run_or_unwired"
    assert results[1]["status"] == "applied"
    assert len(eng.calls) == 1
    assert eng.calls[0].type == "tighten_spread"


@pytest.mark.asyncio
async def test_unwired_engine_is_safe() -> None:
    class Bare:
        pass

    results = await apply_actions_via_engine(
        Bare(),
        [OversightAction(type="drop_market", dry_run=False, reason="x")],
    )
    assert results[0]["status"] == "dry_run_or_unwired"
