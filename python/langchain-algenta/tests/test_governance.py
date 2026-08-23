"""Unit tests for `langchain_algenta.governance.resolve_governed_call` -- the paths that don't
need `langgraph.types.interrupt(...)` (and therefore don't need a real compiled graph) get
covered here directly. The `"pending"` path is exercised in `tests/test_approval_mapping.py`
instead, since `interrupt()` requires a real Pregel task context to do anything but raise.
"""

from __future__ import annotations

import pytest

from langchain_algenta.exceptions import AlgentaToolDenied, AlgentaToolExecutionFailed
from langchain_algenta.governance import resolve_governed_call


async def test_non_envelope_payload_passes_through_unchanged() -> None:
    raw_result = {"capabilities": ["query"]}
    result = await resolve_governed_call("get_contract", raw_result, raw_result, retry=None)
    assert result is raw_result


async def test_successful_receipt_returns_raw_result_unchanged_not_a_parsed_object() -> None:
    payload = {"status": "ok", "code": "ok", "approval_state": "none", "result": {"x": 1}}
    raw_result = ["some", "framework-specific", "wrapper", "around", payload]
    result = await resolve_governed_call("recommend", raw_result, payload, retry=None)
    assert result is raw_result


async def test_denied_by_named_policy_gate_raises_with_receipt_attached() -> None:
    payload = {"status": "error", "code": "plan_hash_mismatch", "approval_state": "none", "plan_hash": "p1"}
    with pytest.raises(AlgentaToolDenied) as exc_info:
        await resolve_governed_call("execute_decision", payload, payload, retry=None)
    assert "plan_hash_mismatch" in str(exc_info.value)
    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.plan_hash == "p1"


async def test_denied_by_rejected_approval_state_raises() -> None:
    payload = {"status": "error", "code": "rejected", "approval_state": "rejected"}
    with pytest.raises(AlgentaToolDenied, match="rejected by policy"):
        await resolve_governed_call("execute_decision", payload, payload, retry=None)


async def test_generic_failure_raises_execution_failed_not_denied() -> None:
    payload = {"status": "error", "code": "upstream_timeout", "approval_state": "none"}
    with pytest.raises(AlgentaToolExecutionFailed) as exc_info:
        await resolve_governed_call("query_data", payload, payload, retry=None)
    assert "upstream_timeout" in str(exc_info.value)
    assert exc_info.value.receipt is not None


async def test_retry_is_never_called_on_a_non_pending_outcome() -> None:
    payload = {"status": "ok", "code": "ok", "approval_state": "none"}
    calls = []

    async def retry() -> tuple[dict, dict]:
        calls.append(1)
        return payload, payload

    await resolve_governed_call("recommend", payload, payload, retry=retry)
    assert calls == []
