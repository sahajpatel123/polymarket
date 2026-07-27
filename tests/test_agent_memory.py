"""Tests for AgentMemory SQLite store."""

from __future__ import annotations

from pathlib import Path

from polymaker.intelligence.memory import (
    MEMORY_KINDS,
    AgentMemory,
    build_messages_with_memory,
)


def test_add_all_kinds_and_recent(tmp_path: Path) -> None:
    mem = AgentMemory(tmp_path / "m.db")
    try:
        for k in sorted(MEMORY_KINDS):
            mem.add(k, f"content for {k}", confidence=0.7, market_or_none="m1")
        recent = mem.get_recent(10)
        assert len(recent) == 4
        kinds = {r.kind for r in recent}
        assert kinds == MEMORY_KINDS
    finally:
        mem.close()


def test_get_for_market_and_search(tmp_path: Path) -> None:
    mem = AgentMemory(tmp_path / "m.db")
    try:
        mem.add("insight", "Newsom illiquid on weekends", market_or_none="newsom", confidence=0.9)
        mem.add("finding", "Fed windows are toxic", market_or_none=None, confidence=0.8)
        mem.add("rule", "Never quote near resolution", market_or_none="other", confidence=0.95)
        mkt = mem.get_for_market("newsom", 10)
        assert any("Newsom" in x.content for x in mkt)
        found = mem.search("toxic", k=5)
        assert any("toxic" in x.content.lower() for x in found)
    finally:
        mem.close()


def test_memory_prefix_parse_and_inject(tmp_path: Path) -> None:
    mem = AgentMemory(tmp_path / "m.db")
    try:
        text = """
Some narrative.
MEMORY: [insight] @cid123 widen spreads on news days
MEMORY: [rule] Never cross the spread
"""
        created = mem.parse_and_store_from_text(text)
        assert len(created) == 2
        assert created[0].kind == "insight"
        assert created[0].market_or_none == "cid123"
        block = mem.inject_for_prompt(query="widen spreads news", market="cid123", k=5)
        assert "Long-term memory" in block
        assert "widen" in block.lower() or "Never" in block
        msgs = build_messages_with_memory(
            mem, system="sys", user="what about news?", query="news", market="cid123"
        )
        assert msgs[0]["role"] == "system"
        assert "memory" in msgs[0]["content"].lower()
    finally:
        mem.close()


def test_boot_reload_last_200(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    mem = AgentMemory(path)
    try:
        for i in range(50):
            mem.add("insight", f"item {i}", confidence=0.5)
    finally:
        mem.close()
    mem2 = AgentMemory(path)
    try:
        cache = mem2.reload_cache(200)
        assert len(cache) == 50
        assert cache[0].content.startswith("item")
    finally:
        mem2.close()


def test_prune_low_confidence_old(tmp_path: Path) -> None:
    import time

    mem = AgentMemory(tmp_path / "m.db")
    try:
        old = time.time() - 40 * 86400
        mem.add("insight", "old junk", confidence=0.1, ts=old)
        mem.add("insight", "fresh", confidence=0.9)
        n = mem.prune()
        assert n >= 1
        recent = mem.get_recent(10)
        assert all(r.content != "old junk" for r in recent)
        assert any(r.content == "fresh" for r in recent)
    finally:
        mem.close()
