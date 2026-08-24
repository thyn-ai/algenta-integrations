"""Unit tests for `pydantic_ai_algenta.receipts` -- no network, no agent, just the models."""

from __future__ import annotations

from pydantic_ai_algenta.receipts import EXECUTION_GATES, ExecutionDenial, ExecutionReceipt, parse_denial, parse_receipt

FULL_RECEIPT = {
    "decision_id": "dec-1",
    "webhook_url": "https://example.com/hooks/dec-1",
    "execution_status": "delivered",
    "response_code": 200,
    "executed_at": "2026-08-23T00:00:00Z",
    "policy_snapshot_id": "policy-snap-1",
    "schema_snapshot_id": "schema-snap-1",
    "manifest_version": "1.0.0",
    "payload_summary": {"decision_id": "dec-1"},
    "safety_overridden": False,
}

FULL_DENIAL_ENVELOPE = {
    "error": {
        "code": "execution_blocked_confidence",
        "gate": "confidence",
        "message": "decision confidence is below policy.min_confidence",
        "override_hint": "Set override_safety=true to bypass the confidence gate.",
    }
}


def test_receipt_round_trips_every_documented_field() -> None:
    receipt = ExecutionReceipt.model_validate(FULL_RECEIPT)
    assert receipt.decision_id == "dec-1"
    assert receipt.webhook_url == "https://example.com/hooks/dec-1"
    assert receipt.execution_status == "delivered"
    assert receipt.response_code == 200
    assert receipt.executed_at == "2026-08-23T00:00:00Z"
    assert receipt.policy_snapshot_id == "policy-snap-1"
    assert receipt.schema_snapshot_id == "schema-snap-1"
    assert receipt.manifest_version == "1.0.0"
    assert receipt.payload_summary == {"decision_id": "dec-1"}
    assert receipt.safety_overridden is False
    # And it dumps back to exactly the same shape it validated from.
    assert receipt.model_dump() == FULL_RECEIPT


def test_receipt_tolerates_unknown_future_fields() -> None:
    envelope = dict(FULL_RECEIPT, engine_build="2026.08.1")
    receipt = ExecutionReceipt.model_validate(envelope)
    assert receipt.model_dump()["engine_build"] == "2026.08.1"


def test_is_delivered_true_for_delivered_status() -> None:
    receipt = ExecutionReceipt.model_validate(FULL_RECEIPT)
    assert receipt.is_delivered()


def test_is_delivered_false_for_failed_status_but_this_is_still_not_a_denial() -> None:
    # A failed downstream webhook delivery is still a *successful* execute_decision call -- the
    # engine did what was asked and is honestly reporting the outcome. Not a policy denial.
    receipt = ExecutionReceipt.model_validate(dict(FULL_RECEIPT, execution_status="failed", response_code=502))
    assert not receipt.is_delivered()


def test_parse_receipt_returns_none_for_a_denial_envelope() -> None:
    assert parse_receipt(FULL_DENIAL_ENVELOPE) is None


def test_parse_receipt_returns_none_for_a_freeform_result_from_another_tool() -> None:
    # e.g. plan_decision's freeform summary, or get_contract's discovery payload.
    assert parse_receipt({"summary": "proposed plan", "scenario": "expand-warehouse"}) is None
    assert parse_receipt({"capabilities": ["query"], "engine_version": "1.4.0"}) is None


def test_parse_receipt_returns_none_for_non_dict() -> None:
    assert parse_receipt("plain string result") is None
    assert parse_receipt(None) is None
    assert parse_receipt(["a", "list"]) is None


def test_parse_receipt_returns_typed_object_for_a_real_receipt() -> None:
    receipt = parse_receipt(FULL_RECEIPT)
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.decision_id == "dec-1"


def test_parse_receipt_honors_a_custom_receipt_model() -> None:
    class CustomReceipt(ExecutionReceipt):
        def shout_status(self) -> str:
            return self.execution_status.upper()

    receipt = parse_receipt(FULL_RECEIPT, model=CustomReceipt)
    assert isinstance(receipt, CustomReceipt)
    assert receipt.shout_status() == "DELIVERED"


def test_parse_denial_returns_typed_object_for_a_real_denial_envelope() -> None:
    denial = parse_denial(FULL_DENIAL_ENVELOPE)
    assert isinstance(denial, ExecutionDenial)
    assert denial.gate == "confidence"
    assert denial.code == "execution_blocked_confidence"
    assert denial.override_hint == "Set override_safety=true to bypass the confidence gate."


def test_parse_denial_returns_none_for_a_successful_receipt() -> None:
    assert parse_denial(FULL_RECEIPT) is None


def test_parse_denial_returns_none_when_error_key_is_missing_or_not_a_dict() -> None:
    assert parse_denial({"summary": "proposed plan"}) is None
    assert parse_denial({"error": "not a dict"}) is None


def test_parse_denial_returns_none_for_non_dict() -> None:
    assert parse_denial("plain string result") is None
    assert parse_denial(None) is None


def test_all_three_real_gates_validate_as_denials() -> None:
    for gate in EXECUTION_GATES:
        envelope = {"error": {"code": f"execution_blocked_{gate}", "gate": gate, "message": "blocked"}}
        denial = parse_denial(envelope)
        assert denial is not None
        assert denial.gate == gate


def test_execution_gates_are_exactly_the_three_real_named_gates() -> None:
    assert EXECUTION_GATES == {"idempotency", "confidence", "risk_floor"}


def test_denial_reason_includes_gate_message_and_override_hint() -> None:
    denial = ExecutionDenial.model_validate(FULL_DENIAL_ENVELOPE["error"])
    reason = denial.denial_reason()
    assert "confidence" in reason
    assert "decision confidence is below policy.min_confidence" in reason
    assert "override_safety=true" in reason


def test_denial_reason_falls_back_to_the_bare_gate_when_no_message() -> None:
    denial = ExecutionDenial.model_validate({"code": "execution_blocked_idempotency", "gate": "idempotency"})
    assert denial.denial_reason() == "idempotency"
