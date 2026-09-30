"""End-to-end scenarios: real `AlgentaToolset` -> real ADK `McpToolset` -> real MCP client ->
real HTTP socket -> the stub Algenta server in `tests/stub_server.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google_adk_algenta import AlgentaExecutionBlocked, AlgentaToolset, ExecutionReceipt
from google_adk_algenta.receipts import parse_receipt

from .helpers import bare_tool_context
from .stub_server import LOW_CONFIDENCE_DECISION_ID, NON_ENVELOPE_RESULT, RISK_FLOOR_DECISION_ID


def _find_tool(tools: list[Any], name: str) -> Any:
    for tool in tools:
        if tool.name == name:
            return tool
    raise AssertionError(f"tool {name!r} not found")


@pytest.mark.anyio
async def test_successful_read_only_recommendation_on_observe_profile(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "recommend")

    result = await tool.run_async(
        args={"scenario": "expand-warehouse"}, tool_context=bare_tool_context()
    )

    # recommend is freeform, not an execute_decision-shaped envelope -- passes through unchanged.
    assert result["structuredContent"]["recommended_action"] == "hold"


@pytest.mark.anyio
async def test_observe_profile_agent_cannot_even_call_execute_decision(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    tools = await toolset.get_tools(bare_tool_context())
    assert "execute_decision" not in {tool.name for tool in tools}


@pytest.mark.anyio
async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't an execute_decision-shaped envelope at all.
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "get_contract")

    result = await tool.run_async(args={}, tool_context=bare_tool_context())

    assert result["structuredContent"] == NON_ENVELOPE_RESULT


@pytest.mark.anyio
async def test_execute_decision_success_returns_a_typed_execution_receipt(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "execute_decision")

    result = await tool.run_async(
        args={"decision_id": "dec-first-call", "webhook_url": "https://example.com/hook"},
        tool_context=bare_tool_context(),
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
async def test_execute_decision_denied_by_the_confidence_gate(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "execute_decision")

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await tool.run_async(
            args={
                "decision_id": LOW_CONFIDENCE_DECISION_ID,
                "webhook_url": "https://example.com/hook",
            },
            tool_context=bare_tool_context(),
        )

    assert "confidence" in str(exc_info.value)
    assert exc_info.value.gate == "confidence"


@pytest.mark.anyio
async def test_execute_decision_denied_by_the_risk_floor_gate(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "execute_decision")

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await tool.run_async(
            args={"decision_id": RISK_FLOOR_DECISION_ID, "webhook_url": "https://example.com/hook"},
            tool_context=bare_tool_context(),
        )

    assert "risk_floor" in str(exc_info.value)
    assert exc_info.value.gate == "risk_floor"


@pytest.mark.anyio
async def test_execute_decision_denied_by_the_idempotency_gate_on_a_repeat_call(
    stub_server: str,
) -> None:
    # The idempotency gate is the one that fires from ordinary use, not a magic sentinel: call
    # the same decision_id twice without force and the second call is blocked.
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "execute_decision")
    args = {"decision_id": "dec-repeat", "webhook_url": "https://example.com/hook"}

    first = await tool.run_async(args=args, tool_context=bare_tool_context())
    assert isinstance(first, ExecutionReceipt)

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await tool.run_async(args=args, tool_context=bare_tool_context())
    assert "idempotency" in str(exc_info.value)
    assert exc_info.value.gate == "idempotency"


@pytest.mark.anyio
async def test_idempotency_gate_is_bypassed_by_a_force_retry(stub_server: str) -> None:
    # AlgentaToolset.run_async unconditionally scrubs force/override_safety before forwarding
    # (see test_never_model_facing.py) -- there is no path through AlgentaToolset itself that
    # ever lets force=True reach the wrapped tool. A human operator's break-glass retry happens
    # outside the model-facing tool call entirely, e.g. over their own direct MCP connection to
    # the same self-hosted engine, which is what this test simulates.
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    tool = _find_tool(tools, "execute_decision")
    decision_id = "dec-force-retry"

    first = await tool.run_async(
        args={"decision_id": decision_id, "webhook_url": "https://example.com/hook"},
        tool_context=bare_tool_context(),
    )
    assert isinstance(first, ExecutionReceipt)

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await tool.run_async(
            args={"decision_id": decision_id, "webhook_url": "https://example.com/hook"},
            tool_context=bare_tool_context(),
        )
    assert "idempotency" in str(exc_info.value)

    operator_toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=stub_server))
    try:
        operator_tools = await operator_toolset.get_tools()
        operator_execute = _find_tool(operator_tools, "execute_decision")
        retried = await operator_execute.run_async(
            args={
                "decision_id": decision_id,
                "webhook_url": "https://example.com/hook",
                "force": True,
            },
            tool_context=bare_tool_context(),
        )
    finally:
        await operator_toolset.close()
    assert retried["structuredContent"]["execution_status"] == "delivered"
    assert retried["structuredContent"]["safety_overridden"] is True
