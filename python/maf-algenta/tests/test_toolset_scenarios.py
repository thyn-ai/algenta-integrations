"""End-to-end scenarios against the real stub Algenta MCP server, driven through a real
`agent_framework.Agent` run loop (via `FakeChatClient`, not a mock of anything in `maf_algenta`
or `agent_framework` itself).

Every scenario below exercises the real `maf_algenta.create_algenta_tools` -> real
`agent_framework.MCPStreamableHTTPTool` -> real `mcp` client -> real wire -> `tests/stub_server.py`
round trip, and the real MAF function-invocation loop (argument validation, exception
propagation) -- nothing about the denial mapping or the never-model-facing scrub is asserted by
inspecting `maf_algenta`'s internals directly; each is proven by actually running an agent and
observing what came back or what was raised.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_framework import Content, MCPStreamableHTTPTool, Message, MiddlewareFailure
from agent_framework.exceptions import ToolExecutionException
from maf_algenta import (
    AlgentaGovernedCallFailure,
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
    create_algenta_tools,
)
from maf_algenta.toolset import (
    _extract_function_result_payload,
    _parse_execution_blocked_from_exception,
)

from .fake_chat_client import FakeChatClient, function_call
from .stub_server import (
    LOW_CONFIDENCE_DECISION_ID,
    LOW_RISK_FLOOR_DECISION_ID,
    MALFORMED_RECEIPT_DECISION_ID,
)


def _chat_response_with_call(name: str, arguments: str, call_id: str) -> Content:
    return function_call(name, arguments, call_id)


def _single_call_agent(tool, *, name: str, arguments: str):
    from agent_framework import ChatResponse

    fake = FakeChatClient(
        [
            ChatResponse(
                messages=Message(role="assistant", contents=[_chat_response_with_call(name, arguments, "c1")])
            ),
            ChatResponse(messages=Message(role="assistant", contents=[Content.from_text("done")])),
        ]
    )
    return fake.as_agent(name="test-agent", tools=[tool])


async def test_query_data_success_via_real_agent_run(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="observe") as tools:
        query_data = next(t for t in tools if t.name == "query_data")
        agent = _single_call_agent(query_data, name="query_data", arguments='{"dataset": "widgets"}')
        result = await agent.run("query widgets")
        assert result.text == "done"
        function_results = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_result"
        ]
        assert function_results and "widgets" in (function_results[0].result or "")


async def test_get_contract_non_envelope_result_passes_through(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="observe") as tools:
        get_contract = next(t for t in tools if t.name == "get_contract")
        # `skip_parsing=True` on the WRAPPED tool bypasses MAF's own outer `parse_result`, so
        # what comes back is exactly what the wrapper returned unchanged: the raw `list[Content]`
        # the real underlying MCP call produced (see `_extract_function_result_payload`'s
        # docstring for why that's the real observed shape, not a dict). Extract the same way the
        # wrapper itself does, to assert on the payload rather than its wire-level `Content`
        # wrapping.
        raw_result = await get_contract.invoke(arguments={}, skip_parsing=True)
        payload = _extract_function_result_payload(raw_result)
        assert payload == {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}


async def test_query_data_success_via_direct_invoke(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="observe") as tools:
        query_data = next(t for t in tools if t.name == "query_data")
        raw_result = await query_data.invoke(arguments={"dataset": "widgets"}, skip_parsing=True)
        payload = _extract_function_result_payload(raw_result)
        assert payload == {"dataset": "widgets", "rows": [{"value": 1}, {"value": 2}]}


async def test_execute_decision_success_round_trip_via_real_agent_run(stub_server: str) -> None:
    """(a) A normal `execute_decision` call round-trips the real `ExecutionReceipt` fields
    correctly -- no approval pause, no gate, just a real receipt reaching the model.
    """
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        # No MAF pre-call approval gate on this tool: the real engine has no async, pre-call
        # approval state to gate on for `execute_decision` (see `maf_algenta.receipts`).
        assert execute_decision.approval_mode != "always_require"

        agent = _single_call_agent(
            execute_decision,
            name="execute_decision",
            arguments='{"decision_id": "decision-success-1", "webhook_url": "https://example.com/hook"}',
        )
        result = await agent.run("execute the decision")
        assert result.text == "done"

        function_results = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_result"
        ]
        assert function_results
        text = function_results[0].result or ""
        assert "decision-success-1" in text
        assert "https://example.com/hook" in text
        assert '"execution_status": "delivered"' in text


async def test_execute_decision_direct_invoke_returns_a_real_receipt(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        raw_result = await execute_decision.invoke(
            arguments={"decision_id": "decision-direct-1", "webhook_url": "https://example.com/hook"},
            skip_parsing=True,
        )
        payload = _extract_function_result_payload(raw_result)
        assert payload["decision_id"] == "decision-direct-1"
        assert payload["webhook_url"] == "https://example.com/hook"
        assert payload["execution_status"] == "delivered"
        assert payload["response_code"] == 200
        assert payload["safety_overridden"] is False
        assert "policy_snapshot_id" in payload
        assert "schema_snapshot_id" in payload
        assert "manifest_version" in payload
        assert "executed_at" in payload


@pytest.mark.parametrize(
    ("decision_id", "expected_gate"),
    [
        (LOW_CONFIDENCE_DECISION_ID, "confidence"),
        (LOW_RISK_FLOOR_DECISION_ID, "risk_floor"),
    ],
)
async def test_execute_decision_denied_by_a_safety_gate_raises_typed_error(
    stub_server: str, decision_id: str, expected_gate: str
) -> None:
    """(b) Each of the confidence/risk_floor gates surfaces as a distinct, typed
    `AlgentaToolDenied` a caller can catch, with the real gate name preserved.
    """
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")

        with pytest.raises(AlgentaToolDenied) as exc_info:
            await execute_decision.invoke(
                arguments={"decision_id": decision_id, "webhook_url": "https://example.com/hook"},
                skip_parsing=True,
            )

        # It's a real, fail-closed agent_framework.MiddlewareFailure -- not a bespoke exception
        # this package invented that a caller could accidentally swallow with a narrower except.
        assert isinstance(exc_info.value, MiddlewareFailure)
        assert isinstance(exc_info.value, AlgentaGovernedCallFailure)
        assert exc_info.value.blocked is not None
        assert exc_info.value.blocked.gate == expected_gate
        assert exc_info.value.blocked.code == f"execution_blocked_{expected_gate}"
        assert expected_gate in str(exc_info.value)


async def test_execute_decision_denied_by_a_safety_gate_via_real_agent_run(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        agent = _single_call_agent(
            execute_decision,
            name="execute_decision",
            arguments=f'{{"decision_id": "{LOW_CONFIDENCE_DECISION_ID}", "webhook_url": "https://example.com/hook"}}',
        )
        with pytest.raises(AlgentaToolDenied) as exc_info:
            await agent.run("execute the decision")
        assert exc_info.value.blocked is not None
        assert exc_info.value.blocked.gate == "confidence"


async def test_execute_decision_denied_by_idempotency_gate_after_first_delivery(stub_server: str) -> None:
    """The `"idempotency"` gate is stateful on the real engine (and on the stub): a decision that
    already delivered once blocks a second, unforced `execute_decision` call for the same
    `decision_id`.
    """
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        args = {"decision_id": "decision-idempotent-1", "webhook_url": "https://example.com/hook"}

        first = await execute_decision.invoke(arguments=args, skip_parsing=True)
        payload = _extract_function_result_payload(first)
        assert payload["execution_status"] == "delivered"

        with pytest.raises(AlgentaToolDenied) as exc_info:
            await execute_decision.invoke(arguments=args, skip_parsing=True)

        assert exc_info.value.blocked is not None
        assert exc_info.value.blocked.gate == "idempotency"
        assert exc_info.value.blocked.code == "execution_blocked_idempotency"


async def test_execute_decision_with_a_malformed_success_payload_raises_typed_failure(stub_server: str) -> None:
    """A non-error `execute_decision` result that doesn't validate as a real `ExecutionReceipt`
    (a real anomaly -- an engine bug, or a version skew this package hasn't caught up with) is
    not silently passed through as if it were a real receipt: it raises the typed
    `AlgentaToolExecutionFailed`, still a real `agent_framework.MiddlewareFailure`.
    """
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        with pytest.raises(AlgentaToolExecutionFailed) as exc_info:
            await execute_decision.invoke(
                arguments={"decision_id": MALFORMED_RECEIPT_DECISION_ID, "webhook_url": "https://example.com/hook"},
                skip_parsing=True,
            )
        assert isinstance(exc_info.value, MiddlewareFailure)
        assert isinstance(exc_info.value, AlgentaGovernedCallFailure)


async def _call_real_execute_decision_directly(base_url: str, **arguments: Any) -> Any:
    """Call the stub server's real `execute_decision` tool directly over its own fresh
    `MCPStreamableHTTPTool` connection -- deliberately *not* through `create_algenta_tools`, whose
    whole point is that `force`/`override_safety` can never reach the real call through it (see
    `test_never_model_facing.py` and `test_smuggled_force_never_reaches_the_real_server_over_the_real_wire`).
    Exercising `force`/`override_safety`'s real effect on the gates themselves -- which only a
    human operator calling the real engine directly, outside the model-facing tool surface, would
    ever do -- needs a connection that doesn't scrub them.
    """
    async with MCPStreamableHTTPTool(name="algenta-admin", url=base_url) as mcp_tool:
        execute_decision = next(fn for fn in mcp_tool.functions if fn.name == "execute_decision")
        return await execute_decision.invoke(arguments=arguments, skip_parsing=True)


async def test_force_bypasses_only_the_idempotency_gate(stub_server: str) -> None:
    """`force=true` re-executes a decision the idempotency gate would otherwise block -- and
    only that gate: it must not smuggle a confidence/risk_floor bypass.
    """
    base_args = {"decision_id": "decision-force-1", "webhook_url": "https://example.com/hook"}
    await _call_real_execute_decision_directly(stub_server, **base_args)

    # A second, forced call re-executes rather than raising the idempotency denial.
    forced = await _call_real_execute_decision_directly(stub_server, **base_args, force=True)
    payload = _extract_function_result_payload(forced)
    assert payload["execution_status"] == "delivered"

    # `force` alone does not bypass the confidence gate.
    with pytest.raises(ToolExecutionException) as exc_info:
        await _call_real_execute_decision_directly(
            stub_server,
            decision_id=LOW_CONFIDENCE_DECISION_ID,
            webhook_url="https://example.com/hook",
            force=True,
        )
    blocked = _parse_execution_blocked_from_exception(exc_info.value)
    assert blocked is not None
    assert blocked.gate == "confidence"


async def test_override_safety_bypasses_only_confidence_and_risk_floor_gates(stub_server: str) -> None:
    """`override_safety=true` bypasses the confidence/risk_floor gates -- and only those: it
    must not smuggle an idempotency bypass.
    """
    confidence_result = await _call_real_execute_decision_directly(
        stub_server,
        decision_id=LOW_CONFIDENCE_DECISION_ID,
        webhook_url="https://example.com/hook",
        override_safety=True,
    )
    payload = _extract_function_result_payload(confidence_result)
    assert payload["execution_status"] == "delivered"
    assert payload["safety_overridden"] is True

    # A decision already delivered still blocks on idempotency even with override_safety --
    # only `force` bypasses that gate.
    with pytest.raises(ToolExecutionException) as exc_info:
        await _call_real_execute_decision_directly(
            stub_server,
            decision_id=LOW_CONFIDENCE_DECISION_ID,
            webhook_url="https://example.com/hook",
            override_safety=True,
        )
    blocked = _parse_execution_blocked_from_exception(exc_info.value)
    assert blocked is not None
    assert blocked.gate == "idempotency"


async def test_smuggled_force_never_reaches_the_real_server_over_the_real_wire(stub_server: str) -> None:
    """Same proof as `test_never_model_facing.py`, but end to end against the real stub server
    (not a fake registry) -- the `force` field really is on the real MCP tool's real advertised
    schema (see `stub_server.execute_decision`'s docstring), and this proves the call-time scrub
    strips it before the real wire call regardless.
    """
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        schema = execute_decision.parameters()
        assert "force" not in schema.get("properties", {})

        agent = _single_call_agent(
            execute_decision,
            name="execute_decision",
            arguments=(
                '{"decision_id": "decision-smuggle-1", "webhook_url": "https://example.com/hook", "force": true}'
            ),
        )
        result = await agent.run("execute the decision")
        assert result.text == "done"

        function_results = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_result"
        ]
        assert function_results
        # The stub's `execute_decision` never saw a prior delivery for this `decision_id`, so a
        # smuggled `force=True` reaching the real call would still succeed either way -- the real
        # proof is `test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call` in
        # `test_never_model_facing.py`, which asserts on the exact received arguments. This test
        # additionally proves the real wire round trip doesn't choke on it and the model still
        # gets a clean receipt back.
        assert '"execution_status": "delivered"' in (function_results[0].result or "")
