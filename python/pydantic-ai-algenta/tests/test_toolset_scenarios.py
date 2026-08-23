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
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from pydantic_ai_algenta import AlgentaToolset, GovernedExecutionReceipt, approve_and_resume
from pydantic_ai_algenta.receipts import parse_receipt

from .helpers import bare_run_context
from .stub_server import NON_ENVELOPE_RESULT, REJECTED_PLAN_CODE, REJECTED_PLAN_HASH


def _last_tool_return(messages: list[Any]) -> ToolReturnPart:
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                return part
    raise AssertionError("no ToolReturnPart found in message history")


async def _approve_via_admin_tool(base_url: str, metadata: dict[str, Any]) -> None:
    """Stand-in for "a human approved this plan through the engine's real HTTP endpoint" --
    calls the stub server's test-only admin tool over its own throwaway `MCPToolset` connection,
    exactly the shape a real `approve` callback passed to `approve_and_resume` would have,
    just pointed at a fake server instead of a real engine.
    """
    admin_toolset = MCPToolset(base_url)
    async with admin_toolset:
        await admin_toolset.direct_call_tool("_test_approve_plan", {"plan_hash": metadata["plan_hash"]})


@pytest.mark.anyio
async def test_successful_read_only_recommendation_on_observe_profile(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    agent = Agent(TestModel(call_tools=["recommend"]), toolsets=[toolset])

    result = await agent.run("what should we do?")

    tool_return = _last_tool_return(result.all_messages())
    assert tool_return.outcome == "success"
    assert isinstance(tool_return.content, GovernedExecutionReceipt)
    assert tool_return.content.approval_state == "none"
    assert tool_return.content.result["recommended_action"] == "hold"


@pytest.mark.anyio
async def test_observe_profile_agent_cannot_even_call_execute_decision(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    assert "execute_decision" not in tools


@pytest.mark.anyio
async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't a governed-execution envelope at all.
    toolset = AlgentaToolset(base_url=stub_server, profile="observe")
    agent = Agent(TestModel(call_tools=["get_contract"]), toolsets=[toolset])

    result = await agent.run("what can this engine do?")

    tool_return = _last_tool_return(result.all_messages())
    assert tool_return.outcome == "success"
    assert tool_return.content == NON_ENVELOPE_RESULT


@pytest.mark.anyio
async def test_execution_denied_outright_by_a_named_policy_gate(stub_server: str) -> None:
    # Deliberately a direct toolset-level call (not a full Agent/TestModel run): this scenario
    # needs a *specific* plan_hash literal (REJECTED_PLAN_HASH) to trigger the gate, which
    # TestModel's schema-driven argument fuzzing can't be steered to produce on demand. Still a
    # real call over the real socket to the real stub server -- only the outer Agent harness is
    # skipped, not the wire.
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]

    result = await toolset.call_tool("execute_decision", {"plan_hash": REJECTED_PLAN_HASH}, ctx, tool)

    from pydantic_ai.tools import ToolDenied

    assert isinstance(result, ToolDenied)
    assert REJECTED_PLAN_CODE in result.message


@pytest.mark.anyio
async def test_execution_paused_for_approval_then_resumed_successfully(stub_server: str) -> None:
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    agent = Agent(
        TestModel(call_tools=["execute_decision"]),
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
    )

    paused = await agent.run("execute the plan")

    assert isinstance(paused.output, DeferredToolRequests)
    assert len(paused.output.approvals) == 1
    call_id = paused.output.approvals[0].tool_call_id
    metadata = paused.output.metadata[call_id]
    assert metadata["execution_id"] is not None
    assert metadata["receipt"]["approval_state"] == "pending"

    resumed = await approve_and_resume(
        agent,
        message_history=paused.all_messages(),
        deferred_requests=paused.output,
        approve=lambda md: _approve_via_admin_tool(stub_server, md),
    )

    tool_return = _last_tool_return(resumed.all_messages())
    assert tool_return.outcome == "success"
    assert isinstance(tool_return.content, GovernedExecutionReceipt)
    assert tool_return.content.approval_state == "approved"
    assert tool_return.content.result == {"executed": True, "plan_hash": metadata["plan_hash"]}


@pytest.mark.anyio
async def test_approval_rejected_by_the_human_never_re_calls_the_engine(stub_server: str) -> None:
    """The human, shown the `DeferredToolRequests`, denies it instead of approving it.

    pydantic-ai resolves a denied approval entirely on its own (see
    `pydantic_ai._tool_execution.build_tool_return_part`'s `ToolDenied` branch) -- the tool is
    never called again, so `AlgentaToolset.call_tool` never runs a second time for this call.
    """
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    agent = Agent(
        TestModel(call_tools=["execute_decision"]),
        toolsets=[toolset],
        output_type=[str, DeferredToolRequests],
    )

    paused = await agent.run("execute the plan")
    assert isinstance(paused.output, DeferredToolRequests)
    call_id = paused.output.approvals[0].tool_call_id

    results = DeferredToolResults()
    results.approvals[call_id] = False
    resumed = await agent.run(message_history=paused.all_messages(), deferred_tool_results=results)

    tool_return = _last_tool_return(resumed.all_messages())
    assert tool_return.outcome == "denied"
    assert tool_return.content == "The tool call was denied."


@pytest.mark.anyio
async def test_receipt_round_trips_through_the_real_wire(stub_server: str) -> None:
    """The typed-fields round trip, exercised over the real wire rather than a hand-built dict."""
    toolset = AlgentaToolset(base_url=stub_server, profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["plan_decision"]

    result = await toolset.call_tool("plan_decision", {"scenario": "expand-warehouse"}, ctx, tool)

    assert isinstance(result, GovernedExecutionReceipt)
    assert result.plan_hash == "plan-expand-warehouse"
    assert result.approval_state == "none"
    assert result.result == {"plan_hash": "plan-expand-warehouse", "rationale": "looks fine"}
    # And parse_receipt agrees when fed the same shape back as a plain dict.
    assert parse_receipt(result.model_dump()) == result
