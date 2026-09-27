"""Unit tests for `temporal_algenta.types` -- the wire dataclasses and the workflow-side
`denial_from_activity_error` chain walker. No server needed.
"""

from __future__ import annotations

from temporal_algenta.types import (
    DENIAL_ERROR_TYPES,
    PROFILE_DENIED_ERROR_TYPE,
    DenialDetails,
    ExecutionReceiptData,
    denial_from_activity_error,
)
from temporalio.exceptions import ApplicationError

DENIAL_DICT = {
    "code": "execution_blocked_confidence",
    "gate": "confidence",
    "message": "Decision confidence is below policy.min_confidence.",
    "override_hint": "Pass override_safety=true to bypass the confidence gate.",
}


def test_denial_details_json_round_trip() -> None:
    details = DenialDetails.from_json_dict(DENIAL_DICT)
    assert details is not None
    assert details.gate == "confidence"
    assert details.to_json_dict() == DENIAL_DICT


def test_denial_details_tolerates_missing_override_hint() -> None:
    value = {k: v for k, v in DENIAL_DICT.items() if k != "override_hint"}
    details = DenialDetails.from_json_dict(value)
    assert details is not None
    assert details.override_hint is None


def test_denial_details_rejects_non_denial_values() -> None:
    assert DenialDetails.from_json_dict("not a dict") is None
    assert DenialDetails.from_json_dict(None) is None
    assert DenialDetails.from_json_dict({"code": "x"}) is None
    assert DenialDetails.from_json_dict({"code": 1, "gate": "x", "message": "y"}) is None


def test_denial_error_types_cover_the_three_real_gates() -> None:
    assert DENIAL_ERROR_TYPES == frozenset(
        {"execution_blocked_idempotency", "execution_blocked_confidence", "execution_blocked_risk_floor"}
    )


def test_denial_from_activity_error_walks_cause_chain() -> None:
    app_error = ApplicationError(
        "blocked", DENIAL_DICT, type="execution_blocked_confidence", non_retryable=True
    )
    activity_error = RuntimeError("activity failed")
    activity_error.__cause__ = app_error
    workflow_error = RuntimeError("workflow failed")
    workflow_error.__cause__ = activity_error

    denial = denial_from_activity_error(workflow_error)
    assert denial is not None
    assert denial.gate == "confidence"
    assert denial.code == "execution_blocked_confidence"


def test_denial_from_activity_error_finds_denial_directly_on_application_error() -> None:
    denial = denial_from_activity_error(
        ApplicationError("blocked", DENIAL_DICT, type="execution_blocked_confidence", non_retryable=True)
    )
    assert denial is not None
    assert denial.gate == "confidence"


def test_denial_from_activity_error_returns_none_for_unrelated_failures() -> None:
    assert denial_from_activity_error(RuntimeError("transport down")) is None
    # An ApplicationError whose details are not a denial body (e.g. a profile refusal, which
    # carries no details at all).
    assert denial_from_activity_error(ApplicationError("refused", type=PROFILE_DENIED_ERROR_TYPE)) is None
    assert (
        denial_from_activity_error(ApplicationError("other", {"unrelated": True}, type="some_other_error"))
        is None
    )


def test_execution_receipt_data_is_wire_shaped() -> None:
    receipt = ExecutionReceiptData(
        decision_id="decision-hold",
        webhook_url="https://ops.example.com/hooks/hold",
        execution_status="delivered",
        response_code=200,
        executed_at="2026-09-01T00:00:00Z",
        policy_snapshot_id="policy-snap-1",
        schema_snapshot_id="schema-snap-1",
        manifest_version="1.0.0",
    )
    assert receipt.is_delivered()
    assert not receipt.safety_overridden
    assert receipt.payload_summary is None
