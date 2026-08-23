"""Unit tests for `approve_and_resume` in isolation from any real agent/toolset/server --
a fake `agent` object records how it was called, so these tests are about the helper's own
bookkeeping (which calls get approved, what gets forwarded to `agent.run`), not about MCP wire
behavior (see `test_toolset_scenarios.py` for the full round trip against the real stub server).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from pydantic_ai_algenta import approve_and_resume


class _FakeAgent:
    def __init__(self) -> None:
        self.run_calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> str:
        self.run_calls.append(kwargs)
        return "resumed"


def _one_pending_call(tool_call_id: str = "call-1") -> DeferredToolRequests:
    call = ToolCallPart(tool_name="execute_decision", args={"plan_hash": "p1"}, tool_call_id=tool_call_id)
    return DeferredToolRequests(approvals=[call], metadata={tool_call_id: {"plan_hash": "p1", "execution_id": "e1"}})


def _history() -> list[ModelRequest]:
    return [ModelRequest(parts=[UserPromptPart(content="go")])]


@pytest.mark.anyio
async def test_approve_and_resume_calls_approve_once_per_pending_call_with_its_metadata() -> None:
    seen_metadata: list[dict[str, Any]] = []

    def approve(metadata: dict[str, Any]) -> None:
        seen_metadata.append(metadata)

    agent = _FakeAgent()
    requests = _one_pending_call()
    result = await approve_and_resume(agent, message_history=_history(), deferred_requests=requests, approve=approve)

    assert result == "resumed"
    assert seen_metadata == [{"plan_hash": "p1", "execution_id": "e1"}]


@pytest.mark.anyio
async def test_approve_and_resume_builds_deferred_tool_results_approving_every_pending_call() -> None:
    agent = _FakeAgent()
    requests = _one_pending_call("call-xyz")
    await approve_and_resume(agent, message_history=_history(), deferred_requests=requests, approve=lambda md: None)

    assert len(agent.run_calls) == 1
    resume_kwargs = agent.run_calls[0]
    deferred_results = resume_kwargs["deferred_tool_results"]
    assert isinstance(deferred_results, DeferredToolResults)
    assert deferred_results.approvals == {"call-xyz": True}


@pytest.mark.anyio
async def test_approve_and_resume_forwards_message_history_and_extra_run_kwargs() -> None:
    agent = _FakeAgent()
    requests = _one_pending_call()
    history = _history()

    await approve_and_resume(
        agent,
        message_history=history,
        deferred_requests=requests,
        approve=lambda md: None,
        usage_limits="sentinel-usage-limits",
    )

    resume_kwargs = agent.run_calls[0]
    assert resume_kwargs["message_history"] is history
    assert resume_kwargs["usage_limits"] == "sentinel-usage-limits"


@pytest.mark.anyio
async def test_approve_and_resume_awaits_an_async_approve_callback() -> None:
    called_with: list[dict[str, Any]] = []

    async def approve(metadata: dict[str, Any]) -> None:
        called_with.append(metadata)

    agent = _FakeAgent()
    requests = _one_pending_call()
    await approve_and_resume(agent, message_history=_history(), deferred_requests=requests, approve=approve)

    assert len(called_with) == 1


@pytest.mark.anyio
async def test_approve_and_resume_propagates_a_raising_approve_callback_without_resuming() -> None:
    def approve(metadata: dict[str, Any]) -> None:
        raise RuntimeError("the real approval endpoint rejected this plan_hash")

    agent = _FakeAgent()
    requests = _one_pending_call()

    with pytest.raises(RuntimeError, match="rejected this plan_hash"):
        await approve_and_resume(agent, message_history=_history(), deferred_requests=requests, approve=approve)

    assert agent.run_calls == []


@pytest.mark.anyio
async def test_approve_and_resume_handles_multiple_pending_approvals() -> None:
    call_a = ToolCallPart(tool_name="execute_decision", args={"plan_hash": "a"}, tool_call_id="call-a")
    call_b = ToolCallPart(tool_name="execute_decision", args={"plan_hash": "b"}, tool_call_id="call-b")
    requests = DeferredToolRequests(
        approvals=[call_a, call_b],
        metadata={"call-a": {"plan_hash": "a"}, "call-b": {"plan_hash": "b"}},
    )
    seen: list[str] = []

    agent = _FakeAgent()
    await approve_and_resume(
        agent,
        message_history=_history(),
        deferred_requests=requests,
        approve=lambda md: seen.append(md["plan_hash"]),
    )

    assert sorted(seen) == ["a", "b"]
    deferred_results = agent.run_calls[0]["deferred_tool_results"]
    assert deferred_results.approvals == {"call-a": True, "call-b": True}
