"""Unit tests for `langchain_algenta.governance.resolve_governed_call` -- no network, no MCP
wire, just the mapping logic keyed on `is_error` and the parsed denial shape.
"""

from __future__ import annotations

import pytest

from langchain_algenta.exceptions import AlgentaExecutionBlocked
from langchain_algenta.governance import resolve_governed_call


async def test_a_success_result_is_returned_unchanged() -> None:
    payload = {"decision_id": "d1", "webhook_url": "https://x", "execution_status": "delivered"}
    raw_result = ["some", "framework-specific", "wrapper", "around", payload]
    result = resolve_governed_call("execute_decision", raw_result, payload, is_error=False)
    assert result is raw_result


async def test_any_other_tools_own_result_shape_passes_through_unchanged_when_not_an_error() -> None:
    # get_contract's discovery payload, log_decision's own result shape, etc. -- none of these
    # need to look like an ExecutionReceipt at all; `is_error=False` is enough to pass through.
    raw_result = {"capabilities": ["query"]}
    result = resolve_governed_call("get_contract", raw_result, raw_result, is_error=False)
    assert result is raw_result


async def test_a_named_gate_denial_raises_with_the_real_gate_and_code_preserved() -> None:
    payload = {"error": {"code": "execution_blocked_confidence", "gate": "confidence", "message": "too risky"}}
    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        resolve_governed_call("execute_decision", payload, payload, is_error=True)
    assert exc_info.value.gate == "confidence"
    assert exc_info.value.denial is not None
    assert exc_info.value.denial.code == "execution_blocked_confidence"
    assert "too risky" in str(exc_info.value)


@pytest.mark.parametrize("gate", ["idempotency", "confidence", "risk_floor"])
async def test_each_of_the_three_real_named_gates_is_recognized(gate: str) -> None:
    payload = {"error": {"code": f"execution_blocked_{gate}", "gate": gate, "message": "blocked"}}
    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        resolve_governed_call("execute_decision", payload, payload, is_error=True)
    assert exc_info.value.gate == gate


async def test_a_denial_body_without_the_error_wrapper_still_parses() -> None:
    # Tolerate either the full `{"error": {...}}` envelope or the inner object directly.
    payload = {"code": "execution_blocked_risk_floor", "gate": "risk_floor", "message": "too risky"}
    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        resolve_governed_call("execute_decision", payload, payload, is_error=True)
    assert exc_info.value.gate == "risk_floor"


async def test_an_error_that_does_not_parse_as_a_named_gate_passes_through_unchanged() -> None:
    # A generic transport failure, or a future error shape this package doesn't recognize yet --
    # not one of the three named gates, so this deliberately does NOT raise an Algenta-specific
    # exception; the caller's own framework (e.g. langchain_mcp_adapters' own isError handling)
    # is left to deal with it exactly as it would for any other tool's error.
    raw_result = "raw framework error value"
    payload = {"some": "unrecognized shape"}
    result = resolve_governed_call("query_data", raw_result, payload, is_error=True)
    assert result is raw_result


def test_resolve_governed_call_has_no_retry_or_pending_related_parameters() -> None:
    # The real tool has exactly two outcomes (success, or one of three named gates), decided
    # synchronously in one call -- there is nothing left to retry after a pause, so this
    # function's signature should carry no `retry`/interrupt-resume-shaped parameter at all, and
    # should not even need to be a coroutine anymore (nothing here ever awaits anything).
    import inspect

    signature = inspect.signature(resolve_governed_call)
    assert "retry" not in signature.parameters
    assert not inspect.iscoroutinefunction(resolve_governed_call)
