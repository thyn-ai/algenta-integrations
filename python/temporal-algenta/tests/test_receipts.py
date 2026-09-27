"""Unit tests for `temporal_algenta.receipts` -- the shared receipt/denial envelopes and their
tolerant parsers. Mirrors the shape of the sibling packages' receipt tests; no server needed.
"""

from __future__ import annotations

import json

from temporal_algenta.receipts import (
    NAMED_EXECUTION_GATES,
    ExecutionDenial,
    ExecutionReceipt,
    parse_denial,
    parse_receipt,
)

RECEIPT_PAYLOAD = {
    "decision_id": "decision-hold",
    "webhook_url": "https://ops.example.com/hooks/hold",
    "execution_status": "delivered",
    "response_code": 200,
    "executed_at": "2026-09-01T00:00:00Z",
    "policy_snapshot_id": "policy-snap-1",
    "schema_snapshot_id": "schema-snap-1",
    "manifest_version": "1.0.0",
    "payload_summary": {"decision_id": "decision-hold"},
    "safety_overridden": False,
}

DENIAL_BODY = {
    "code": "execution_blocked_risk_floor",
    "gate": "risk_floor",
    "message": "risk_p5 is below -policy.risk_floor.",
    "override_hint": "Pass override_safety=true to bypass the risk floor gate.",
}


def test_parse_receipt_round_trip() -> None:
    receipt = parse_receipt(RECEIPT_PAYLOAD)
    assert receipt is not None
    assert receipt.decision_id == "decision-hold"
    assert receipt.is_delivered()
    assert not receipt.safety_overridden


def test_parse_receipt_failed_delivery_is_still_a_receipt() -> None:
    receipt = parse_receipt(RECEIPT_PAYLOAD | {"execution_status": "failed", "response_code": 503})
    assert receipt is not None
    assert not receipt.is_delivered()


def test_parse_receipt_tolerates_extra_engine_fields() -> None:
    receipt = parse_receipt(RECEIPT_PAYLOAD | {"brand_new_engine_field": 1})
    assert receipt is not None
    assert receipt.model_extra["brand_new_engine_field"] == 1


def test_parse_receipt_rejects_other_tools_payloads() -> None:
    # get_contract / log_decision / plan_decision payloads are not receipts -- the deliberate
    # signal for "this wasn't an execute_decision success".
    assert parse_receipt({"capabilities": ["query"], "engine_version": "1.4.0"}) is None
    assert parse_receipt({"decision_id": "decision-hold", "chosen_action": "hold"}) is None
    assert parse_receipt({"plan_id": "plan-x"}) is None
    assert parse_receipt("not a dict") is None
    assert parse_receipt(None) is None


def test_parse_denial_from_inner_object_and_envelope() -> None:
    assert parse_denial(DENIAL_BODY) is not None
    assert parse_denial({"error": DENIAL_BODY}) is not None


def test_parse_denial_from_prose_wrapped_text() -> None:
    # The real FastMCP path wraps the raised body's JSON in its own prose.
    text = f"Error executing tool execute_decision: {json.dumps({'error': DENIAL_BODY})}"
    denial = parse_denial(text)
    assert denial is not None
    assert denial.gate == "risk_floor"
    assert denial.code == "execution_blocked_risk_floor"
    assert denial.override_hint is not None


def test_parse_denial_tolerates_missing_override_hint() -> None:
    body = {k: v for k, v in DENIAL_BODY.items() if k != "override_hint"}
    denial = parse_denial(body)
    assert denial is not None
    assert denial.override_hint is None


def test_parse_denial_rejects_non_denial_shapes() -> None:
    assert parse_denial({"error": "not an object"}) is None
    assert parse_denial({"code": 1, "gate": "x"}) is None
    assert parse_denial("no json here") is None
    assert parse_denial(None) is None
    assert parse_denial(42) is None


def test_denial_gate_is_str_not_literal_for_forward_compatibility() -> None:
    denial = parse_denial(DENIAL_BODY | {"gate": "some_future_gate"})
    assert denial is not None
    assert denial.gate not in NAMED_EXECUTION_GATES


def test_models_are_independent_types() -> None:
    receipt = ExecutionReceipt.model_validate(RECEIPT_PAYLOAD)
    denial = ExecutionDenial.model_validate(DENIAL_BODY)
    assert receipt.decision_id != denial.code
