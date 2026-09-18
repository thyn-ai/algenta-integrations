"""End-to-end scenarios: real `create_algenta_tools` -> real `llama_index.tools.mcp.BasicMCPClient`
-> real HTTP socket -> the stub Algenta server in `tests/stub_server.py`.

Uses `tests/helpers.py`'s `ScriptedFunctionCallingLLM` to script a real `FunctionAgent.run()`
deterministically for the scenarios that go through a full agent loop, and a direct
`tool.acall(...)` for scenarios that only need to call a wrapped tool once.
"""

from __future__ import annotations

import pytest
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.agent.workflow.workflow_events import ToolCallResult
from llama_index.core.llms.llm import ToolSelection
from llamaindex_algenta import (
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
    ExecutionReceipt,
    create_algenta_tools,
)

from .helpers import ScriptedFunctionCallingLLM
from .stub_server import LOW_CONFIDENCE_DECISION_ID, NON_ENVELOPE_RESULT, RISK_FLOOR_DECISION_ID


@pytest.mark.asyncio
async def test_successful_read_only_recommendation(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    tool = next(t for t in tools if t.metadata.name == "recommend")

    output = await tool.acall(scenario="expand-warehouse")

    assert output.raw_output["recommended_action"] == "hold"


@pytest.mark.asyncio
async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't a governed shape at all -- it's not even
    # execute_decision, so it's never even considered for receipt/denial parsing.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    tool = next(t for t in tools if t.metadata.name == "get_contract")

    output = await tool.acall()

    assert output.raw_output == NON_ENVELOPE_RESULT


@pytest.mark.asyncio
async def test_plan_decision_and_log_decision_are_plain_ungated_passthroughs(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="govern")

    plan_tool = next(t for t in tools if t.metadata.name == "plan_decision")
    plan_output = await plan_tool.acall(scenario="expand-warehouse")
    assert plan_output.raw_output == {
        "plan_id": "plan-expand-warehouse",
        "scenario": "expand-warehouse",
        "summary": "looks fine",
    }

    log_tool = next(t for t in tools if t.metadata.name == "log_decision")
    log_output = await log_tool.acall(chosen_action="expand-warehouse", confidence=0.9, expected_value=1000.0)
    assert log_output.raw_output["decision_id"] == "decision-expand-warehouse"
    assert log_output.raw_output["chosen_action"] == "expand-warehouse"


@pytest.mark.asyncio
async def test_execute_decision_succeeds_and_returns_a_real_execution_receipt(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    output = await tool.acall(decision_id="decision-fresh", webhook_url="https://example.com/hook")

    receipt = output.raw_output
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.decision_id == "decision-fresh"
    assert receipt.webhook_url == "https://example.com/hook"
    assert receipt.execution_status == "delivered"
    assert receipt.response_code == 200
    assert receipt.safety_overridden is False
    assert receipt.policy_snapshot_id is not None
    assert receipt.schema_snapshot_id is not None
    assert receipt.manifest_version is not None
    assert receipt.executed_at is not None


@pytest.mark.asyncio
async def test_execute_decision_idempotency_gate_blocks_a_repeat_delivery(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    first = await tool.acall(decision_id="decision-once", webhook_url="https://example.com/hook")
    assert isinstance(first.raw_output, ExecutionReceipt)

    with pytest.raises(AlgentaToolDenied) as exc_info:
        await tool.acall(decision_id="decision-once", webhook_url="https://example.com/hook")

    assert exc_info.value.gate == "idempotency"
    assert exc_info.value.code == "execution_blocked_idempotency"
    assert exc_info.value.override_hint is not None
    assert "idempotency" in str(exc_info.value)


@pytest.mark.asyncio
async def test_execute_decision_confidence_gate_blocks_a_low_confidence_decision(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    with pytest.raises(AlgentaToolDenied) as exc_info:
        await tool.acall(decision_id=LOW_CONFIDENCE_DECISION_ID, webhook_url="https://example.com/hook")

    assert exc_info.value.gate == "confidence"
    assert exc_info.value.code == "execution_blocked_confidence"


@pytest.mark.asyncio
async def test_execute_decision_risk_floor_gate_blocks_an_overly_risky_decision(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    with pytest.raises(AlgentaToolDenied) as exc_info:
        await tool.acall(decision_id=RISK_FLOOR_DECISION_ID, webhook_url="https://example.com/hook")

    assert exc_info.value.gate == "risk_floor"
    assert exc_info.value.code == "execution_blocked_risk_floor"


@pytest.mark.asyncio
async def test_mcp_protocol_level_call_error_raises_execution_failed_not_a_silent_passthrough(stub_server: str) -> None:
    # `blow_up` isn't part of the real contract -- it exists purely to exercise the MCP
    # protocol-level isError=True path (a server-side tool crash the MCP SDK already turned into
    # ordinary, non-raising response data). `full` profile is used since `blow_up` isn't in any
    # real contract profile.
    tools = await create_algenta_tools(base_url=stub_server, profile="full")
    tool = next(t for t in tools if t.metadata.name == "blow_up")

    with pytest.raises(AlgentaToolExecutionFailed) as exc_info:
        await tool.acall()

    assert "arbitrary-tool-failure" in str(exc_info.value)
    assert exc_info.value.gate is None


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


@pytest.mark.asyncio
async def test_denied_execute_decision_through_a_real_function_agent_run_does_not_crash_the_run(
    stub_server: str,
) -> None:
    """A real `FunctionAgent.run()` swallows `AlgentaToolDenied` into an ordinary
    `ToolOutput(is_error=True, exception=...)` -- there is no pause/interrupt anywhere in this
    path: the call is blocked synchronously, in the same tool-call step, and the run finishes
    normally on the very first turn. A caller must inspect the `ToolCallResult`, not wrap
    `agent.run()` in a `try`/`except`.
    """
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    llm = ScriptedFunctionCallingLLM(
        turns=[
            [
                ToolSelection(
                    tool_id="call-1",
                    tool_name="execute_decision",
                    tool_kwargs={"decision_id": LOW_CONFIDENCE_DECISION_ID, "webhook_url": "https://example.com/hook"},
                )
            ],
            "I saw the denial and stopped.",
        ]
    )
    agent = FunctionAgent(tools=tools, llm=llm)

    handler = agent.run(user_msg="execute the plan")
    tool_call_results: list[ToolCallResult] = []
    async for event in handler.stream_events():
        if isinstance(event, ToolCallResult):
            tool_call_results.append(event)
    result = await handler  # does NOT raise, and does NOT pause -- this is the whole point being proven

    assert result.response.content == "I saw the denial and stopped."
    assert len(tool_call_results) == 1
    tool_output = tool_call_results[0].tool_output
    assert tool_output.is_error is True
    assert isinstance(tool_output.exception, AlgentaToolDenied)
    assert tool_output.exception.gate == "confidence"


@pytest.mark.asyncio
async def test_successful_execute_decision_through_a_real_function_agent_run_completes_in_one_turn(
    stub_server: str,
) -> None:
    """The positive-path mirror of the test above: a real agent run that succeeds also finishes
    in a single tool-call step, with no `InputRequiredEvent`/pause of any kind ever emitted --
    there is nothing asynchronous about `execute_decision`'s real outcome.
    """
    from llama_index.core.workflow import InputRequiredEvent

    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    llm = ScriptedFunctionCallingLLM(
        turns=[
            [
                ToolSelection(
                    tool_id="call-1",
                    tool_name="execute_decision",
                    tool_kwargs={"decision_id": "decision-agent-run", "webhook_url": "https://example.com/hook"},
                )
            ],
            "Done: executed.",
        ]
    )
    agent = FunctionAgent(tools=tools, llm=llm)

    handler = agent.run(user_msg="please execute the plan")
    tool_call_results: list[ToolCallResult] = []
    saw_input_required = False
    async for event in handler.stream_events():
        if isinstance(event, ToolCallResult):
            tool_call_results.append(event)
        if isinstance(event, InputRequiredEvent):
            saw_input_required = True
    result = await handler

    assert saw_input_required is False
    assert result.response.content == "Done: executed."
    assert len(tool_call_results) == 1
    tool_output = tool_call_results[0].tool_output
    assert tool_output.is_error is False
    assert isinstance(tool_output.raw_output, ExecutionReceipt)
    assert tool_output.raw_output.execution_status == "delivered"
