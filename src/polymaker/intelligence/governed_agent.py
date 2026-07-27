"""Bridge between LLMGovernance and the GrokAgent.

Wraps an existing ``GrokAgent`` so every call goes through
``LLMGovernance.check_and_log`` before the response reaches the
caller. The agent itself doesn't know about governance — this is
the *only* integration point.

Usage in the engine::

    agent = GrokAgent(api_key=...)
    gov = LLMGovernance(capital_usdc=500, log_path=...)
    wrapped = GovernedGrokAgent(agent, gov)
    response = await wrapped.chat(messages, tools=...)
    if response.approved:
        apply(response.actions)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from polymaker.intelligence.agent import AgentResponse, GrokAgent
from polymaker.intelligence.llm_governance import GovernanceDecision, LLMGovernance


@dataclass(frozen=True)
class GovernedResponse:
    """The result of a governed LLM call.

    Combines the raw ``AgentResponse`` (so callers can still see the
    content / tool calls for logging) with the ``GovernanceDecision``
    (the audit verdict).
    """

    agent_response: AgentResponse
    governance: GovernanceDecision
    context: dict[str, Any]


class GovernedGrokAgent:
    """Wraps a ``GrokAgent`` with mandatory governance.

    The wrapper preserves the agent's interface (it forwards ``chat``
    and ``chat_json_tool``) but routes every response through
    ``LLMGovernance.check_and_log``. The returned ``GovernedResponse``
    tells the caller whether the LLM output was approved.
    """

    def __init__(self, agent: GrokAgent, governance: LLMGovernance) -> None:
        self._agent = agent
        self._gov = governance

    @property
    def agent(self) -> GrokAgent:
        return self._agent

    @property
    def governance(self) -> LLMGovernance:
        return self._gov

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.2,
        kind: str = "chat",
        context: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> GovernedResponse:
        started = time.time()
        resp = await self._agent.chat(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            kind=kind,
            extra_body=extra_body,
        )
        # Build a structured representation the governance can audit.
        llm_payload = self._extract_actions(resp)
        prompt_text = self._extract_prompt(messages)
        decision = self._gov.check_and_log(
            prompt=prompt_text,
            response=llm_payload,
            llm_started_at=started,
            context=context,
        )
        return GovernedResponse(
            agent_response=resp,
            governance=decision,
            context=context or {},
        )

    async def chat_json_tool(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_name: str,
        tool_schema: dict[str, Any],
        kind: str = "tool",
        description: str = "",
        context: dict[str, Any] | None = None,
    ) -> GovernedResponse:
        started = time.time()
        parsed, resp = await self._agent.chat_json_tool(
            messages,
            tool_name=tool_name,
            tool_schema=tool_schema,
            kind=kind,
            description=description,
        )
        # Wrap the parsed dict in an ``actions`` envelope so the
        # governance layer's direction / size / risk checks fire.
        response_payload: dict[str, Any]
        if isinstance(parsed, dict):
            response_payload = {"actions": parsed}
        else:
            response_payload = {"content": str(parsed)}
        decision = self._gov.check_and_log(
            prompt=self._extract_prompt(messages),
            response=response_payload,
            llm_started_at=started,
            context=context,
        )
        return GovernedResponse(
            agent_response=resp,
            governance=decision,
            context=context or {},
        )

    def record_llm_fill(self, pnl_usdc: float) -> None:
        """Tell governance that an LLM-influenced trade filled."""
        self._gov.record_llm_fill(pnl_usdc)

    # ── Internals ─────────────────────────────────────────────────

    @staticmethod
    def _extract_actions(resp: AgentResponse) -> dict[str, Any]:
        """Pull a structured payload out of an AgentResponse for governance.

        Tool-call arguments are flattened into a single ``actions`` dict
        so the governance layer can scan them for forbidden fields like
        ``side`` or ``size_pct`` without needing to know about the
        tool-call structure.

        If multiple tool calls set the same key, the last one wins.
        That's acceptable for governance: the LLM should not be setting
        these fields at all.
        """
        if resp.tool_calls:
            actions: dict[str, Any] = {}
            for tc in resp.tool_calls:
                if isinstance(tc.arguments, dict):
                    actions.update(tc.arguments)
            return {
                "actions": actions,
                "content": resp.content,
                "_tool_call_names": [tc.name for tc in resp.tool_calls],
            }
        return {"content": resp.content}

    @staticmethod
    def _extract_prompt(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            parts.append(f"[{role}] {content}")
        return "\n".join(parts)
