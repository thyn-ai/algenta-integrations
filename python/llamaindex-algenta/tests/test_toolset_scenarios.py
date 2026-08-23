"""End-to-end scenarios: real `create_algenta_tools` -> real `llama_index.tools.mcp.BasicMCPClient`
-> real HTTP socket -> the stub Algenta server in `tests/stub_server.py`.

Uses `tests/helpers.py`'s `ScriptedFunctionCallingLLM` to script a real `FunctionAgent.run()`
deterministically for the scenarios that go through a full agent loop, and `bare_context()` for
scenarios that only need to call a wrapped tool directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.agent.workflow.workflow_events import ToolCallResult
from llama_index.core.llms.llm import ToolSelection

from llamaindex_algenta import (
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
    GovernedExecutionReceipt,
    create_algenta_tools,
)

from .helpers import ScriptedFunctionCallingLLM, bare_context
from .stub_server import NON_ENVELOPE_RESULT, REJECTED_PLAN_CODE, REJECTED_PLAN_HASH


@pytest.mark.asyncio
async def test_successful_read_only_recommendation(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    tool = next(t for t in tools if t.metadata.name == "recommend")

    output = await tool.acall(ctx=bare_context(), scenario="expand-warehouse")

    assert isinstance(output.raw_output, GovernedExecutionReceipt)
    assert output.raw_output.approval_state == "none"
    assert output.raw_output.result["recommended_action"] == "hold"


@pytest.mark.asyncio
async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't a governed-execution envelope at all.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    tool = next(t for t in tools if t.metadata.name == "get_contract")

    output = await tool.acall(ctx=bare_context())

    assert output.raw_output == NON_ENVELOPE_RESULT


@pytest.mark.asyncio
async def test_execution_denied_outright_by_a_named_policy_gate(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    with pytest.raises(AlgentaToolDenied) as exc_info:
        await tool.acall(ctx=bare_context(), plan_hash=REJECTED_PLAN_HASH)

    assert REJECTED_PLAN_CODE in str(exc_info.value)
    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.plan_hash == REJECTED_PLAN_HASH


@pytest.mark.asyncio
async def test_mcp_protocol_level_call_error_raises_execution_failed_not_a_silent_passthrough(stub_server: str) -> None:
    # `blow_up` isn't part of the real contract -- it exists purely to exercise the MCP
    # protocol-level isError=True path (a server-side tool crash the MCP SDK already turned into
    # ordinary, non-raising response data). `full` profile is used since `blow_up` isn't in any
    # real contract profile.
    tools = await create_algenta_tools(base_url=stub_server, profile="full")
    tool = next(t for t in tools if t.metadata.name == "blow_up")

    with pytest.raises(AlgentaToolExecutionFailed) as exc_info:
        await tool.acall(ctx=bare_context())

    assert "arbitrary-tool-failure" in str(exc_info.value)
    # No governed-execution receipt was ever parsed for this outcome -- there was nothing to
    # parse, only an MCP protocol-level error.
    assert exc_info.value.receipt is None


@pytest.mark.asyncio
async def test_receipt_round_trips_through_the_real_wire(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="govern")
    tool = next(t for t in tools if t.metadata.name == "plan_decision")

    output = await tool.acall(ctx=bare_context(), scenario="expand-warehouse")

    result = output.raw_output
    assert isinstance(result, GovernedExecutionReceipt)
    assert result.plan_hash == "plan-expand-warehouse"
    assert result.approval_state == "none"
    assert result.result == {"plan_hash": "plan-expand-warehouse", "rationale": "looks fine"}


@pytest.mark.asyncio
async def test_successful_call_through_a_real_function_agent_run(stub_server: str) -> None:
    """Same success scenario, but through a real `FunctionAgent.run()` rather than a direct
    `tool.acall(...)` -- proves the tool built by `create_algenta_tools` behaves correctly inside
    the ordinary agent-loop path too, not just when called in isolation.
    """
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    llm = ScriptedFunctionCallingLLM(
        turns=[
            [ToolSelection(tool_id="call-1", tool_name="recommend", tool_kwargs={"scenario": "expand-warehouse"})],
            "Here's my recommendation.",
        ]
    )
    agent = FunctionAgent(tools=tools, llm=llm)

    handler = agent.run(user_msg="what should we do?")
    tool_call_results: list[ToolCallResult] = []
    async for event in handler.stream_events():
        if isinstance(event, ToolCallResult):
            tool_call_results.append(event)
    result = await handler

    assert result.response.content == "Here's my recommendation."
    assert len(tool_call_results) == 1
    assert tool_call_results[0].tool_output.is_error is False
    assert isinstance(tool_call_results[0].tool_output.raw_output, GovernedExecutionReceipt)


@pytest.mark.asyncio
async def test_denied_call_through_a_real_function_agent_run_does_not_crash_the_run(stub_server: str) -> None:
    """A real `FunctionAgent.run()` swallows `AlgentaToolDenied` into an ordinary
    `ToolOutput(is_error=True, exception=...)` -- verified live, documented in
    `llamaindex_algenta.exceptions`. A caller must inspect the `ToolCallResult`, not wrap
    `agent.run()` in a `try`/`except`.
    """
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    llm = ScriptedFunctionCallingLLM(
        turns=[
            [ToolSelection(tool_id="call-1", tool_name="execute_decision", tool_kwargs={"plan_hash": REJECTED_PLAN_HASH})],
            "I saw the denial and stopped.",
        ]
    )
    agent = FunctionAgent(tools=tools, llm=llm)

    handler = agent.run(user_msg="execute the plan")
    tool_call_results: list[ToolCallResult] = []
    async for event in handler.stream_events():
        if isinstance(event, ToolCallResult):
            tool_call_results.append(event)
    result = await handler  # does NOT raise -- this is the whole point being proven

    assert result.response.content == "I saw the denial and stopped."
    assert len(tool_call_results) == 1
    tool_output = tool_call_results[0].tool_output
    assert tool_output.is_error is True
    assert isinstance(tool_output.exception, AlgentaToolDenied)
