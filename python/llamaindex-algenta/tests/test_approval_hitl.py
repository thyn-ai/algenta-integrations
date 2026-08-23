"""Proves the real approval mapping for a pending `execute_decision` receipt, end to end:

1. A real `FunctionAgent.run()` genuinely pauses mid-tool-call via `Context.wait_for_event()`
   (the same primitive this package's own research probes -- `probe_hitl2.py`/`probe_hitl3.py`
   under `scratchpad/li_venv_legacy/tests/` -- first proved live against installed
   `llama-index-core` 0.14.24 / `llama-index-workflows` 2.23.3).
2. Resuming with a `HumanResponseEvent` causes the *entire tool-call step to replay from the
   top* -- verified here by observing that the underlying MCP `execute_decision` call actually
   happens a second time (the stub server's plan only becomes `approval_state="approved"` once a
   human calls `_test_approve_plan` out of band, so a successful second-call result is only
   possible if the step really redid the whole wrapped call, not just resumed mid-function).
3. Calling the same wrapped tool directly (via `bare_context()`, with no live running workflow)
   instead raises `AlgentaApprovalStillPending` as a fail-closed fallback, rather than hanging or
   silently succeeding -- there is no live step for `wait_for_event()` to actually pause.
"""

from __future__ import annotations

import pytest
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.agent.workflow.workflow_events import ToolCallResult
from llama_index.core.llms.llm import ToolSelection
from llama_index.core.workflow import HumanResponseEvent, InputRequiredEvent
from llama_index.tools.mcp import BasicMCPClient

from llamaindex_algenta import AlgentaApprovalStillPending, GovernedExecutionReceipt, create_algenta_tools

from .helpers import ScriptedFunctionCallingLLM, bare_context
from .stub_server import PENDING_PLAN_HASH


@pytest.mark.asyncio
async def test_pending_approval_pauses_the_real_agent_run_then_resumes_after_out_of_band_approval(
    stub_server: str,
) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    llm = ScriptedFunctionCallingLLM(
        turns=[
            [ToolSelection(tool_id="call-1", tool_name="execute_decision", tool_kwargs={"plan_hash": PENDING_PLAN_HASH})],
            "Done: executed after approval.",
        ]
    )
    agent = FunctionAgent(tools=tools, llm=llm)

    handler = agent.run(user_msg="please execute the plan")

    saw_input_required = False
    tool_call_results: list[ToolCallResult] = []
    async for event in handler.stream_events():
        if isinstance(event, ToolCallResult):
            tool_call_results.append(event)
        if isinstance(event, InputRequiredEvent):
            saw_input_required = True
            assert event.plan_hash == PENDING_PLAN_HASH
            assert event.tool_name == "execute_decision"

            # The out-of-band approval: a human/policy engine calling the engine's REAL approval
            # endpoint. Stands in for that here via the stub server's own test-only admin tool,
            # over a separate, throwaway MCP connection -- exactly the shape a real caller's own
            # out-of-band approval call would have, just pointed at a fake server.
            admin_client = BasicMCPClient(stub_server)
            await admin_client.call_tool("_test_approve_plan", {"plan_hash": PENDING_PLAN_HASH})

            handler.ctx.send_event(HumanResponseEvent(response="approved", plan_hash=PENDING_PLAN_HASH))

    result = await handler
    assert saw_input_required
    assert result.response.content == "Done: executed after approval."

    # The real proof that the whole step -- MCP round trip included -- was replayed on resume,
    # not just resumed mid-function: the ONLY way `execute_decision` ever reports
    # `approval_state="approved"` from this stub server is a SECOND real call after
    # `_test_approve_plan` ran, since the first call (before approval) came back "pending". A
    # single `ToolCallResult` with a genuinely-approved receipt is only possible if the entire
    # wrapped tool call -- including its `client.call_tool(...)` -- really did run twice.
    assert len(tool_call_results) == 1
    tool_output = tool_call_results[0].tool_output
    assert tool_output.is_error is False
    receipt = tool_output.raw_output
    assert isinstance(receipt, GovernedExecutionReceipt)
    assert receipt.approval_state == "approved"
    assert receipt.result == {"executed": True, "plan_hash": PENDING_PLAN_HASH}


@pytest.mark.asyncio
async def test_calling_the_wrapped_tool_directly_with_no_live_workflow_fails_closed(stub_server: str) -> None:
    """No `FunctionAgent`/`Workflow.run()` anywhere -- `ctx` is a bare, never-run `Context`, so
    `wait_for_event()` has nothing live to pause and raises `ContextStateError`, which this
    package maps onto `AlgentaApprovalStillPending` rather than hanging or silently succeeding.
    """
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    with pytest.raises(AlgentaApprovalStillPending) as exc_info:
        await tool.acall(ctx=bare_context(), plan_hash=PENDING_PLAN_HASH)

    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.approval_state == "pending"
    assert exc_info.value.receipt.plan_hash == PENDING_PLAN_HASH


@pytest.mark.asyncio
async def test_a_still_pending_receipt_after_timeout_also_fails_closed(stub_server: str) -> None:
    """Same fail-closed exception, different real cause: a live pause that nobody ever resumes
    within `approval_wait_timeout` raises `asyncio.TimeoutError` inside `wait_for_event()`, which
    this package also maps onto `AlgentaApprovalStillPending`.
    """
    tools = await create_algenta_tools(base_url=stub_server, profile="execute", approval_wait_timeout=0.05)
    llm = ScriptedFunctionCallingLLM(
        turns=[
            [ToolSelection(tool_id="call-1", tool_name="execute_decision", tool_kwargs={"plan_hash": PENDING_PLAN_HASH})],
            # FunctionAgent asks the LLM again after seeing the tool's (swallowed) error result --
            # this second turn is that follow-up, not a second attempt at the tool call itself.
            "I saw the timeout and stopped.",
        ]
    )
    agent = FunctionAgent(tools=tools, llm=llm)

    handler = agent.run(user_msg="please execute the plan")
    tool_call_results: list[ToolCallResult] = []
    async for event in handler.stream_events():
        if isinstance(event, ToolCallResult):
            tool_call_results.append(event)
    await handler

    assert len(tool_call_results) == 1
    tool_output = tool_call_results[0].tool_output
    assert tool_output.is_error is True
    assert isinstance(tool_output.exception, AlgentaApprovalStillPending)
