"""End-to-end scenarios: real `AlgentaToolset` -> real `pydantic_ai.mcp.MCPToolset` -> real
`fastmcp` client -> real HTTP socket -> the stub Algenta server in `tests/stub_server.py`.

Uses `pydantic_ai.models.test.TestModel` to script the agent's tool-calling behavior
deterministically, per this package's testing convention (see `tests/conftest.py`'s
`ALLOW_MODEL_REQUESTS = False` guard -- no test here can accidentally call a live model).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, ToolDenied
from pydantic_ai_algenta import AlgentaToolset, ExecutionReceipt
from pydantic_ai_algenta.receipts import parse_receipt

from .helpers import bare_run_context
from .stub_server import LOW_CONFIDENCE_DECISION_ID, NON_ENVELOPE_RESULT, RISK_FLOOR_DECISION_ID


def _last_tool_return(messages: list[Any]) -> ToolReturnPart:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                return part
    raise AssertionError("no ToolReturnPart found in message history")


@pytest.mark.anyio
async def test_successful_read_only_recommendation_on_observe_profile(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    agent = Agent(TestModel(call_tools=["recommend"]), toolsets=[toolset])

    result = await agent.run("what should we do?")

    tool_return = _last_tool_return(result.all_messages())
    assert tool_return.outcome == "success"
    # recommend is freeform, not an execute_decision-shaped envelope -- passes through unchanged.
    assert tool_return.content["recommended_action"] == "hold"


@pytest.mark.anyio
async def test_observe_profile_agent_cannot_even_call_execute_decision(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    assert "execute_decision" not in tools


@pytest.mark.anyio
async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't an execute_decision-shaped envelope at all.
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    agent = Agent(TestModel(call_tools=["get_contract"]), toolsets=[toolset])

    result = await agent.run("what can this engine do?")

    tool_return = _last_tool_return(result.all_messages())
    assert tool_return.outcome == "success"
    assert tool_return.content == NON_ENVELOPE_RESULT


@pytest.mark.anyio
async def test_execute_decision_success_returns_a_typed_execution_receipt(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]

    result = await toolset.call_tool(
        "execute_decision", {"decision_id": "dec-first-call", "webhook_url": "https://example.com/hook"}, ctx, tool
    )

    assert isinstance(result, ExecutionReceipt)
    assert result.decision_id == "dec-first-call"
    assert result.webhook_url == "https://example.com/hook"
    assert result.execution_status == "delivered"
    assert result.is_delivered()
    assert result.safety_overridden is False
    # And parse_receipt agrees when fed the same shape back as a plain dict.
    assert parse_receipt(result.model_dump()) == result


@pytest.mark.anyio
async def test_execute_decision_is_synchronous_and_never_pauses_for_approval(stub_server: str) -> None:
    # There is no "pending approval" state on the real execute_decision -- a call either
    # succeeds or is denied in the very same call. Confirm the run completes in one step and
    # never surfaces a DeferredToolRequests, even when output_type would permit one.
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    agent = Agent(
        TestModel(call_tools=["execute_decision"]),
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
    )

    result = await agent.run("execute the plan")

    assert not isinstance(result.output, DeferredToolRequests)
    tool_return = _last_tool_return(result.all_messages())
    assert tool_return.outcome == "success"


@pytest.mark.anyio
async def test_execute_decision_denied_by_the_confidence_gate(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]

    result = await toolset.call_tool(
        "execute_decision",
        {"decision_id": LOW_CONFIDENCE_DECISION_ID, "webhook_url": "https://example.com/hook"},
        ctx,
        tool,
    )

    assert isinstance(result, ToolDenied)
    assert "confidence" in result.message


@pytest.mark.anyio
async def test_execute_decision_denied_by_the_risk_floor_gate(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]

    result = await toolset.call_tool(
        "execute_decision",
        {"decision_id": RISK_FLOOR_DECISION_ID, "webhook_url": "https://example.com/hook"},
        ctx,
        tool,
    )

    assert isinstance(result, ToolDenied)
    assert "risk_floor" in result.message


@pytest.mark.anyio
async def test_execute_decision_denied_by_the_idempotency_gate_on_a_repeat_call(stub_server: str) -> None:
    # The idempotency gate is the one that fires from ordinary use, not a magic sentinel: call
    # the same decision_id twice without force and the second call is blocked.
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]
    args = {"decision_id": "dec-repeat", "webhook_url": "https://example.com/hook"}

    first = await toolset.call_tool("execute_decision", args, ctx, tool)
    assert isinstance(first, ExecutionReceipt)

    second = await toolset.call_tool("execute_decision", args, ctx, tool)
    assert isinstance(second, ToolDenied)
    assert "idempotency" in second.message


@pytest.mark.anyio
async def test_idempotency_gate_is_bypassed_by_a_force_retry(stub_server: str) -> None:
    # AlgentaToolset.call_tool unconditionally scrubs force/override_safety before forwarding
    # (see test_never_model_facing.py) -- there is no path through AlgentaToolset itself that
    # ever lets force=True reach the wrapped tool. A human operator's break-glass retry happens
    # outside the model-facing tool call entirely, e.g. over their own direct MCP connection to
    # the same self-hosted engine, which is what this test simulates.
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]
    decision_id = "dec-force-retry"

    first = await toolset.call_tool(
        "execute_decision", {"decision_id": decision_id, "webhook_url": "https://example.com/hook"}, ctx, tool
    )
    assert isinstance(first, ExecutionReceipt)

    second = await toolset.call_tool(
        "execute_decision", {"decision_id": decision_id, "webhook_url": "https://example.com/hook"}, ctx, tool
    )
    assert isinstance(second, ToolDenied)
    assert "idempotency" in second.message

    operator_toolset = MCPToolset(stub_server)
    async with operator_toolset:
        retried = await operator_toolset.direct_call_tool(
            "execute_decision",
            {"decision_id": decision_id, "webhook_url": "https://example.com/hook", "force": True},
        )
    assert retried["execution_status"] == "delivered"
    assert retried["safety_overridden"] is True
