"""The `"pending"` approval-mapping path: `AlgentaToolCallInterceptor` pauses the enclosing
LangGraph run via `langgraph.types.interrupt(...)` rather than raising or returning a sentinel.

This needs a *real*, compiled LangGraph graph with a real checkpointer to exercise meaningfully
-- `langgraph.types.interrupt()` raises a plain `RuntimeError` ("Called get_config outside of a
runnable context") when there's no Pregel task context at all, verified directly against the
installed `langgraph` package (see `tests/helpers.build_single_tool_call_graph`'s docstring and
`test_pending_approval_outside_any_graph_context_raises_runtime_error` below for that contrasting,
equally-real case) -- so a bare `tool.ainvoke(...)` can't exercise the pause/resume behavior at
all, only the "no graph, no pause" honesty case.
"""

from __future__ import annotations

import pytest
from langgraph.types import Command, Interrupt

from langchain_algenta import AlgentaApprovalStillPending, create_algenta_tools
from langchain_algenta.receipts import parse_receipt

from .helpers import build_single_tool_call_graph
from .stub_server import PENDING_PLAN_HASH


async def test_pending_approval_pauses_the_graph_with_the_receipts_identifying_fields(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")
    graph, config = build_single_tool_call_graph(execute_decision, {"plan_hash": PENDING_PLAN_HASH})

    result = await graph.ainvoke({"messages": []}, config)

    interrupts = result["__interrupt__"]
    assert len(interrupts) == 1
    interrupt_obj = interrupts[0]
    assert isinstance(interrupt_obj, Interrupt)
    payload = interrupt_obj.value
    assert payload["reason"] == "algenta_pending_approval"
    assert payload["tool_name"] == "execute_decision"
    assert payload["plan_hash"] == PENDING_PLAN_HASH
    assert payload["execution_id"] == f"exec-{PENDING_PLAN_HASH}"
    assert payload["receipt"]["approval_state"] == "pending"


async def test_resuming_after_the_engine_approves_returns_a_fresh_successful_receipt(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")
    graph, config = build_single_tool_call_graph(execute_decision, {"plan_hash": PENDING_PLAN_HASH})

    paused = await graph.ainvoke({"messages": []}, config)
    assert "__interrupt__" in paused

    # A human (or a policy-engine node reading `__interrupt__`) goes and records the real,
    # out-of-band approval against plan_hash -- simulated here via the stub server's test-only
    # admin tool, exactly the shape a real approval action would have, just pointed at a fake
    # server instead of a real engine.
    admin_tools = await create_algenta_tools(base_url=stub_server, profile="full")
    approve = next(t for t in admin_tools if t.name == "_test_approve_plan")
    await approve.ainvoke({"plan_hash": PENDING_PLAN_HASH})

    # Resuming with Command(resume=...) continues the *same* paused tool call.
    # AlgentaToolCallInterceptor's own contract is to retry the exact same call once after
    # resume -- not to re-interpret the resume value itself as the outcome.
    resumed = await graph.ainvoke(Command(resume="approved-by-human"), config)

    assert "__interrupt__" not in resumed
    final_message = resumed["messages"][-1]
    # The node under test (see helpers.build_single_tool_call_graph) stashes `repr(result)` of
    # the tool's raw content-block result into the message; the receipt payload is embedded in
    # it as JSON text, same shape as a real chat model would receive.
    payload = _extract_receipt_payload_from_repr(final_message.content)
    receipt = parse_receipt(payload)
    assert receipt is not None
    assert receipt.approval_state == "approved"
    assert receipt.result == {"executed": True, "plan_hash": PENDING_PLAN_HASH, "forced": False}


async def test_resuming_without_the_engine_ever_approving_raises_still_pending(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")
    graph, config = build_single_tool_call_graph(execute_decision, {"plan_hash": PENDING_PLAN_HASH})

    paused = await graph.ainvoke({"messages": []}, config)
    assert "__interrupt__" in paused

    # Resume without ever actually approving the plan on the engine side -- the retry the
    # interceptor performs after resume still comes back "pending", and the interceptor only
    # retries once (to avoid pausing forever), so this must surface as a clear, named failure
    # rather than silently looking like success or pausing again.
    with pytest.raises(AlgentaApprovalStillPending) as exc_info:
        await graph.ainvoke(Command(resume="approved-by-human"), config)

    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.plan_hash == PENDING_PLAN_HASH


async def test_pending_approval_outside_any_graph_context_raises_an_error_not_a_graceful_pause(
    stub_server: str,
) -> None:
    """The honest "no graph, no pause" case -- see the package README's "What `interrupt()`
    needs" section. `AlgentaToolCallInterceptor` always calls `langgraph.types.interrupt(...)`
    unconditionally on a pending approval; whether that becomes a graceful pause or a raised
    error is entirely determined by whether the caller's tool call is running inside a real
    LangGraph Pregel task (i.e. inside a compiled graph), not by anything this package decides.

    Calling `tool.ainvoke(...)` bare (no graph at all) still runs inside *some* LangChain
    `Runnable` config context (`BaseTool` is itself a `Runnable`), so `langgraph.types.interrupt()`
    gets far enough to look for LangGraph's own Pregel-scoped scratchpad key and fail with a
    `KeyError` on that specific key -- not the coarser `RuntimeError("Called get_config outside
    of a runnable context")` a truly bare call to `interrupt()` with no `Runnable` context
    whatsoever raises. Both are verified, real, unhandled propagations of whatever
    `langgraph.types.interrupt()` itself raises (per `langchain_core.tools.base`'s own exception
    propagation -- only `ToolException` gets specially swallowed); this test asserts the specific
    one this package's actual call path (a real `BaseTool.ainvoke()`) produces, not the doc
    example's bare-function-call one.
    """
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")

    with pytest.raises(KeyError, match="pregel_scratchpad"):
        await execute_decision.ainvoke({"plan_hash": PENDING_PLAN_HASH})


def _extract_receipt_payload_from_repr(content: str) -> object:
    import ast
    import json

    # `repr([{"type": "text", "text": "..."}])` -- a Python list-of-dicts repr; `ast.literal_eval`
    # safely parses it back without executing anything.
    blocks = ast.literal_eval(content)
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            return json.loads(block["text"])
    raise AssertionError(f"no text content block found in {content!r}")
