"""Unit tests for `temporal_algenta.errors` -- the denial -> Temporal `ApplicationError`
mapping. No server needed.
"""

from __future__ import annotations

from temporal_algenta.errors import (
    AlgentaExecutionBlocked,
    AlgentaToolCallFailed,
    AlgentaToolDenied,
    denial_application_error,
)
from temporal_algenta.receipts import ExecutionDenial
from temporal_algenta.types import DenialDetails
from temporalio.exceptions import ApplicationError

DENIAL = ExecutionDenial(
    code="execution_blocked_idempotency",
    gate="idempotency",
    message="Decision 'decision-hold' was already delivered.",
    override_hint="Pass force=true to override the idempotency gate for one re-execution.",
)


def test_denial_application_error_shape() -> None:
    error = denial_application_error(DENIAL)
    assert isinstance(error, ApplicationError)
    # The engine's own denial code becomes the Temporal error type, so RetryPolicy's
    # non_retryable_error_types and workflow-side except logic can key off it.
    assert error.type == "execution_blocked_idempotency"
    assert error.non_retryable is True
    assert "idempotency" in str(error)

    details = [d for d in error.details]
    assert len(details) == 1
    parsed = DenialDetails.from_json_dict(details[0])
    assert parsed is not None
    assert (parsed.code, parsed.gate, parsed.message) == (DENIAL.code, DENIAL.gate, DENIAL.message)
    assert parsed.override_hint == DENIAL.override_hint


def test_execution_blocked_exposes_gate_shortcut() -> None:
    error = AlgentaExecutionBlocked("blocked", denial=DENIAL)
    assert error.gate == "idempotency"
    assert error.denial is DENIAL


def test_execution_blocked_without_denial_has_no_gate() -> None:
    assert AlgentaExecutionBlocked("blocked", denial=None).gate is None


def test_tool_denied_and_tool_call_failed_carry_context() -> None:
    denied = AlgentaToolDenied("execute_decision is not part of the 'observe' profile")
    assert denied.denial is None

    failed = AlgentaToolCallFailed("engine restarted mid-query", payload="engine restarted mid-query")
    assert failed.denial is None
    assert failed.payload == "engine restarted mid-query"
