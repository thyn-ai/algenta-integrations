"""The real `execute_decision` outcome: a synchronous success (`ExecutionReceipt`) or a
synchronous denial on exactly one of three named policy gates (`AlgentaExecutionBlocked`) --
decided in the same call, every time. There is no "pending approval" outcome for this tool at
all, and therefore nothing here needs a compiled LangGraph graph, a checkpointer, or
`langgraph.types.interrupt(...)`/`Command(resume=...)` -- unlike this file's predecessor,
`test_approval_mapping.py`, which this file replaces outright (that file's entire premise, a
pending/interrupt/resume round trip keyed on a `plan_hash`, never corresponded to anything the
real engine returns; see `langchain_algenta.governance`'s module docstring for the full account).

Exercises the real `create_algenta_tools` -> real `langchain_mcp_adapters.MultiServerMCPClient`
-> real `mcp` client -> real HTTP socket -> `tests/stub_server.py` round trip, so a wire-shape
regression in the denial mapping would actually be caught here.
"""

from __future__ import annotations

import pytest

from langchain_algenta import AlgentaExecutionBlocked, create_algenta_tools
from langchain_algenta.receipts import parse_receipt

from .stub_server import (
    BELOW_RISK_FLOOR_DECISION_ID,
    CONFIDENCE_GATE_CODE,
    FAILED_DELIVERY_DECISION_ID,
    IDEMPOTENCY_GATE_CODE,
    LOW_CONFIDENCE_DECISION_ID,
    RISK_FLOOR_GATE_CODE,
)


async def _get_execute_decision(stub_server: str):
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    return next(t for t in tools if t.name == "execute_decision")


async def test_a_clean_call_returns_a_real_execution_receipt(stub_server: str) -> None:
    execute_decision = await _get_execute_decision(stub_server)

    raw_result = await execute_decision.ainvoke({"decision_id": "decision-1", "webhook_url": "https://example.com/hook"})

    payload = _first_text_json(raw_result)
    receipt = parse_receipt(payload)
    assert receipt is not None
    assert receipt.decision_id == "decision-1"
    assert receipt.webhook_url == "https://example.com/hook"
    assert receipt.execution_status == "delivered"
    assert receipt.is_delivered()
    assert receipt.response_code == 200
    assert receipt.safety_overridden is False


async def test_a_failed_webhook_delivery_is_still_a_real_receipt_not_a_denial(stub_server: str) -> None:
    # execution_status="failed" means the webhook target rejected delivery -- the call itself
    # completed. This must never be confused with one of the three real policy-gate denials.
    execute_decision = await _get_execute_decision(stub_server)

    raw_result = await execute_decision.ainvoke(
        {"decision_id": FAILED_DELIVERY_DECISION_ID, "webhook_url": "https://example.com/hook"}
    )

    payload = _first_text_json(raw_result)
    receipt = parse_receipt(payload)
    assert receipt is not None
    assert receipt.execution_status == "failed"
    assert not receipt.is_delivered()
    assert receipt.response_code == 503


async def test_idempotency_gate_blocks_a_repeat_call_on_the_same_decision_id(stub_server: str) -> None:
    execute_decision = await _get_execute_decision(stub_server)
    args = {"decision_id": "decision-repeat", "webhook_url": "https://example.com/hook"}

    first = await execute_decision.ainvoke(args)
    assert parse_receipt(_first_text_json(first)) is not None

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await execute_decision.ainvoke(args)

    assert exc_info.value.gate == "idempotency"
    assert exc_info.value.denial is not None
    assert exc_info.value.denial.code == IDEMPOTENCY_GATE_CODE
    assert exc_info.value.denial.override_hint is not None


async def test_confidence_gate_blocks_a_low_confidence_decision(stub_server: str) -> None:
    execute_decision = await _get_execute_decision(stub_server)

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await execute_decision.ainvoke(
            {"decision_id": LOW_CONFIDENCE_DECISION_ID, "webhook_url": "https://example.com/hook"}
        )

    assert exc_info.value.gate == "confidence"
    assert exc_info.value.denial.code == CONFIDENCE_GATE_CODE


async def test_risk_floor_gate_blocks_a_too_risky_decision(stub_server: str) -> None:
    execute_decision = await _get_execute_decision(stub_server)

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await execute_decision.ainvoke(
            {"decision_id": BELOW_RISK_FLOOR_DECISION_ID, "webhook_url": "https://example.com/hook"}
        )

    assert exc_info.value.gate == "risk_floor"
    assert exc_info.value.denial.code == RISK_FLOOR_GATE_CODE


async def test_a_blocked_call_never_produces_an_interrupt_or_a_pending_result(stub_server: str) -> None:
    # The honest, corrected behavior: a blocked call raises immediately, synchronously, from
    # this one `ainvoke()` -- it never returns a "pending" result and never needs a LangGraph
    # checkpointer or a `Command(resume=...)` round trip to get an answer.
    execute_decision = await _get_execute_decision(stub_server)

    with pytest.raises(AlgentaExecutionBlocked):
        await execute_decision.ainvoke(
            {"decision_id": LOW_CONFIDENCE_DECISION_ID, "webhook_url": "https://example.com/hook"}
        )
    # No graph, no checkpointer, no interrupt -- a bare `ainvoke()` call is enough to observe the
    # full, final outcome of a blocked `execute_decision` call.


def _first_text_json(raw_result: object) -> object:
    """Unwrap the JSON payload from a real MCP tool call's LangChain content-block result."""
    import json

    if isinstance(raw_result, list):
        for block in raw_result:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block["text"])
    raise AssertionError(f"expected a list of text content blocks, got {raw_result!r}")
