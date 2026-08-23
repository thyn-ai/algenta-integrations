"""End-to-end scenarios against the real stub Algenta MCP server, driven through a real
`agent_framework.Agent` run loop (via `FakeChatClient`, not a mock of anything in `maf_algenta`
or `agent_framework` itself).

Every scenario below exercises the real `maf_algenta.create_algenta_tools` -> real
`agent_framework.MCPStreamableHTTPTool` -> real `mcp` client -> real wire -> `tests/stub_server.py`
round trip, and the real MAF function-invocation loop (approval gate, argument validation,
exception propagation) -- nothing about the approval pause, the denial, the failure, or the
never-model-facing scrub is asserted by inspecting `maf_algenta`'s internals directly; each is
proven by actually running an agent and observing what came back or what was raised.
"""

from __future__ import annotations

import pytest
from agent_framework import Content, MCPStreamableHTTPTool, Message, MiddlewareFailure

from maf_algenta import (
    AlgentaApprovalStillPending,
    AlgentaGovernedCallFailure,
    AlgentaToolDenied,
    create_algenta_tools,
)

from maf_algenta.toolset import _extract_function_result_payload

from .fake_chat_client import FakeChatClient, function_call
from .stub_server import PENDING_PLAN_HASH, REJECTED_PLAN_HASH


def _chat_response_with_call(name: str, arguments: str, call_id: str) -> Content:
    return function_call(name, arguments, call_id)


async def _approve_plan_out_of_band(base_url: str, plan_hash: str) -> None:
    """Simulate "a human approved this plan via the engine's real HTTP endpoint" by calling the
    stub server's test-only administrative tool directly -- deliberately *not* through
    `create_algenta_tools` (that tool isn't part of the real contract and would never be exposed
    to a model), mirroring exactly how a real out-of-band approval call would bypass the
    model-facing tool surface entirely.
    """
    async with MCPStreamableHTTPTool(name="algenta-admin", url=base_url) as mcp_tool:
        approve_fn = next(fn for fn in mcp_tool.functions if fn.name == "_test_approve_plan")
        await approve_fn.invoke(arguments={"plan_hash": plan_hash}, skip_parsing=True)


async def test_query_data_success_via_real_agent_run(stub_server: str) -> None:
    from agent_framework import ChatResponse

    async with create_algenta_tools(base_url=stub_server, profile="observe") as tools:
        query_data = next(t for t in tools if t.name == "query_data")
        fake = FakeChatClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[_chat_response_with_call("query_data", '{"dataset": "widgets"}', "c1")],
                    )
                ),
                ChatResponse(messages=Message(role="assistant", contents=[Content.from_text("done")])),
            ]
        )
        agent = fake.as_agent(name="test-agent", tools=[query_data])
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
        # what comes back is exactly what `_governed_call` returned unchanged: the raw
        # `list[Content]` the real underlying MCP call produced (see
        # `_extract_function_result_payload`'s docstring for why that's the real observed shape,
        # not a dict). Extract the same way the wrapper itself does, to assert on the payload
        # rather than its wire-level `Content` wrapping.
        raw_result = await get_contract.invoke(arguments={}, skip_parsing=True)
        payload = _extract_function_result_payload(raw_result)
        # Not a governed-execution receipt at all -- returned exactly as the server sent it.
        assert payload == {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}


async def test_query_data_success_via_direct_invoke(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="observe") as tools:
        query_data = next(t for t in tools if t.name == "query_data")
        raw_result = await query_data.invoke(arguments={"dataset": "widgets"}, skip_parsing=True)
        payload = _extract_function_result_payload(raw_result)
        assert payload["status"] == "ok"
        assert payload["result"] == {"dataset": "widgets", "rows": [{"value": 1}, {"value": 2}]}


async def test_execute_decision_pauses_for_approval_via_real_agent_run(stub_server: str) -> None:
    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        assert execute_decision.approval_mode == "always_require"

        from agent_framework import ChatResponse

        fake = FakeChatClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[
                            function_call(
                                "execute_decision",
                                f'{{"plan_hash": "{PENDING_PLAN_HASH}", "idempotency_key": "idem-1"}}',
                                "c1",
                            )
                        ],
                    )
                ),
            ]
        )
        agent = fake.as_agent(name="test-agent", tools=[execute_decision])
        result = await agent.run("execute the plan")
        approval_requests = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_approval_request"
        ]
        assert approval_requests, "expected the run to pause for human approval, not call the tool yet"


async def test_execute_decision_still_pending_after_approval_gate_raises(stub_server: str) -> None:
    """The honest, verified finding this package's design is built on: MAF's `approval_mode`
    pre-call gate and the engine's own `approval_state` are orthogonal. Approving the *call*
    doesn't retroactively record the *plan's* out-of-band policy approval on the engine, so the
    receipt still comes back `"pending"` -- and since MAF has no resumable pause primitive at
    this layer, that's a fail-closed `AlgentaApprovalStillPending`.
    """
    from agent_framework import ChatResponse

    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        fake = FakeChatClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[
                            function_call(
                                "execute_decision",
                                f'{{"plan_hash": "{PENDING_PLAN_HASH}", "idempotency_key": "idem-1"}}',
                                "c1",
                            )
                        ],
                    )
                ),
            ]
        )
        agent = fake.as_agent(name="test-agent", tools=[execute_decision])
        result = await agent.run("execute the plan")
        approval_requests = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_approval_request"
        ]
        approval_response = Content.from_function_approval_response(
            id=approval_requests[0].id, function_call=approval_requests[0].function_call, approved=True
        )
        resumed_history = list(result.messages) + [Message(role="user", contents=[approval_response])]

        with pytest.raises(AlgentaApprovalStillPending) as exc_info:
            await agent.run(resumed_history)

        # It's a real, fail-closed agent_framework.MiddlewareFailure -- not a bespoke exception
        # this package invented that a caller could accidentally swallow with a narrower except.
        assert isinstance(exc_info.value, MiddlewareFailure)
        assert isinstance(exc_info.value, AlgentaGovernedCallFailure)
        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt.plan_hash == PENDING_PLAN_HASH
        assert exc_info.value.receipt.approval_state == "pending"


async def test_execute_decision_denied_after_approval_gate_raises(stub_server: str) -> None:
    from agent_framework import ChatResponse

    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        fake = FakeChatClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[
                            function_call(
                                "execute_decision",
                                f'{{"plan_hash": "{REJECTED_PLAN_HASH}", "idempotency_key": "idem-1"}}',
                                "c1",
                            )
                        ],
                    )
                ),
            ]
        )
        agent = fake.as_agent(name="test-agent", tools=[execute_decision])
        result = await agent.run("execute the plan")
        approval_requests = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_approval_request"
        ]
        approval_response = Content.from_function_approval_response(
            id=approval_requests[0].id, function_call=approval_requests[0].function_call, approved=True
        )
        resumed_history = list(result.messages) + [Message(role="user", contents=[approval_response])]

        with pytest.raises(AlgentaToolDenied) as exc_info:
            await agent.run(resumed_history)

        assert isinstance(exc_info.value, MiddlewareFailure)
        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt.code == "stale_plan"
        assert "stale_plan" in str(exc_info.value)


async def test_execute_decision_full_success_round_trip_after_out_of_band_approval(stub_server: str) -> None:
    from agent_framework import ChatResponse

    plan_hash = "plan-needs-approval-full-success"
    await _approve_plan_out_of_band(stub_server, plan_hash)

    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        fake = FakeChatClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[
                            function_call(
                                "execute_decision", f'{{"plan_hash": "{plan_hash}", "idempotency_key": "idem-1"}}', "c1"
                            )
                        ],
                    )
                ),
                ChatResponse(messages=Message(role="assistant", contents=[Content.from_text("done")])),
            ]
        )
        agent = fake.as_agent(name="test-agent", tools=[execute_decision])
        result = await agent.run("execute the plan")
        approval_requests = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_approval_request"
        ]
        approval_response = Content.from_function_approval_response(
            id=approval_requests[0].id, function_call=approval_requests[0].function_call, approved=True
        )
        resumed_history = list(result.messages) + [Message(role="user", contents=[approval_response])]

        resumed = await agent.run(resumed_history)
        assert resumed.text == "done"

        function_results = [
            c for m in resumed.messages for c in m.contents if getattr(c, "type", None) == "function_result"
        ]
        assert function_results, "expected the real tool call to have actually happened this time"


async def test_smuggled_force_never_reaches_the_real_server_over_the_real_wire(stub_server: str) -> None:
    """Same proof as `test_never_model_facing.py`, but end to end against the real stub server
    (not a fake registry) -- the `force` field really is on the real MCP tool's real advertised
    schema (see `stub_server.execute_decision`'s docstring), and this proves the call-time scrub
    strips it before the real wire call regardless.
    """
    from agent_framework import ChatResponse

    plan_hash = "plan-needs-approval-force-scrub"
    await _approve_plan_out_of_band(stub_server, plan_hash)

    async with create_algenta_tools(base_url=stub_server, profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        schema = execute_decision.parameters()
        assert "force" not in schema.get("properties", {})

        fake = FakeChatClient(
            [
                ChatResponse(
                    messages=Message(
                        role="assistant",
                        contents=[
                            function_call(
                                "execute_decision",
                                f'{{"plan_hash": "{plan_hash}", "idempotency_key": "idem-1", "force": true}}',
                                "c1",
                            )
                        ],
                    )
                ),
                ChatResponse(messages=Message(role="assistant", contents=[Content.from_text("done")])),
            ]
        )
        agent = fake.as_agent(name="test-agent", tools=[execute_decision])
        result = await agent.run("execute the plan")
        approval_requests = [
            c for m in result.messages for c in m.contents if getattr(c, "type", None) == "function_approval_request"
        ]
        approval_response = Content.from_function_approval_response(
            id=approval_requests[0].id, function_call=approval_requests[0].function_call, approved=True
        )
        resumed_history = list(result.messages) + [Message(role="user", contents=[approval_response])]
        resumed = await agent.run(resumed_history)

        function_results = [
            c for m in resumed.messages for c in m.contents if getattr(c, "type", None) == "function_result"
        ]
        assert function_results
        # `stub_server.execute_decision` echoes the `force` value it actually received in
        # `result.forced` -- proving the real underlying MCP call saw `force=False` (its own
        # default), never the model-smuggled `force=True`.
        assert '"forced": false' in (function_results[0].result or "")
