"""Unit tests for `langchain_algenta.receipts` -- no network, no graph, just the models."""

from __future__ import annotations

from langchain_algenta.receipts import NAMED_EXECUTION_GATES, ExecutionDenial, ExecutionReceipt, parse_denial, parse_receipt

FULL_RECEIPT = {
    "decision_id": "decision-1",
    "webhook_url": "https://example.com/hook",
    "execution_status": "delivered",
    "response_code": 200,
    "executed_at": "2026-08-23T00:00:00Z",
    "policy_snapshot_id": "policy-snap-1",
    "schema_snapshot_id": "schema-snap-1",
    "manifest_version": "1.0.0",
    "payload_summary": {"decision_id": "decision-1"},
    "safety_overridden": False,
}


def test_receipt_round_trips_every_documented_field() -> None:
    receipt = ExecutionReceipt.model_validate(FULL_RECEIPT)
    assert receipt.decision_id == "decision-1"
    assert receipt.webhook_url == "https://example.com/hook"
    assert receipt.execution_status == "delivered"
    assert receipt.response_code == 200
    assert receipt.executed_at == "2026-08-23T00:00:00Z"
    assert receipt.policy_snapshot_id == "policy-snap-1"
    assert receipt.schema_snapshot_id == "schema-snap-1"
    assert receipt.manifest_version == "1.0.0"
    assert receipt.payload_summary == {"decision_id": "decision-1"}
    assert receipt.safety_overridden is False
    # And it dumps back to exactly the same shape it validated from.
    assert receipt.model_dump() == FULL_RECEIPT


def test_receipt_tolerates_unknown_future_fields() -> None:
    envelope = dict(FULL_RECEIPT, engine_build="2026.08.1")
    receipt = ExecutionReceipt.model_validate(envelope)
    assert receipt.model_dump()["engine_build"] == "2026.08.1"


def test_is_delivered_true_for_a_delivered_execution_status() -> None:
    receipt = ExecutionReceipt.model_validate(FULL_RECEIPT)
    assert receipt.is_delivered()


def test_is_delivered_false_for_a_failed_execution_status() -> None:
    receipt = ExecutionReceipt.model_validate(dict(FULL_RECEIPT, execution_status="failed", response_code=503))
    assert not receipt.is_delivered()


def test_safety_overridden_survives_the_round_trip() -> None:
    receipt = ExecutionReceipt.model_validate(dict(FULL_RECEIPT, safety_overridden=True))
    assert receipt.safety_overridden is True


def test_parse_receipt_returns_none_for_any_other_tools_own_result_shape() -> None:
    # get_contract's discovery payload, and log_decision's own real result shape -- neither
    # carries webhook_url/execution_status/response_code, so neither validates.
    assert parse_receipt({"capabilities": ["query"], "engine_version": "1.4.0"}) is None
    assert parse_receipt({"decision_id": "d1", "chosen_action": "hold", "confidence": 0.9}) is None


def test_parse_receipt_returns_none_for_non_dict() -> None:
    assert parse_receipt("plain string result") is None
    assert parse_receipt(None) is None
    assert parse_receipt(["a", "list"]) is None


def test_parse_receipt_returns_typed_object_for_a_real_envelope() -> None:
    receipt = parse_receipt(FULL_RECEIPT)
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.decision_id == "decision-1"


def test_parse_receipt_honors_a_custom_receipt_model() -> None:
    class CustomReceipt(ExecutionReceipt):
        def shout_status(self) -> str:
            return self.execution_status.upper()

    receipt = parse_receipt(FULL_RECEIPT, model=CustomReceipt)
    assert isinstance(receipt, CustomReceipt)
    assert receipt.shout_status() == "DELIVERED"


# -- ExecutionDenial -----------------------------------------------------------------------------

FULL_DENIAL_ENVELOPE = {
    "error": {
        "code": "execution_blocked_confidence",
        "gate": "confidence",
        "message": "Decision confidence is below policy.min_confidence.",
        "override_hint": "Pass override_safety=true to bypass the confidence gate.",
    }
}


def test_parse_denial_accepts_the_full_error_envelope() -> None:
    denial = parse_denial(FULL_DENIAL_ENVELOPE)
    assert isinstance(denial, ExecutionDenial)
    assert denial.code == "execution_blocked_confidence"
    assert denial.gate == "confidence"
    assert denial.message == "Decision confidence is below policy.min_confidence."
    assert denial.override_hint == "Pass override_safety=true to bypass the confidence gate."


def test_parse_denial_accepts_the_inner_object_directly() -> None:
    denial = parse_denial(FULL_DENIAL_ENVELOPE["error"])
    assert isinstance(denial, ExecutionDenial)
    assert denial.gate == "confidence"


def test_parse_denial_returns_none_for_a_non_dict() -> None:
    assert parse_denial("plain string") is None
    assert parse_denial(None) is None
    assert parse_denial(["a", "list"]) is None


def test_parse_denial_returns_none_for_a_shape_missing_required_fields() -> None:
    assert parse_denial({"error": {"message": "no code or gate here"}}) is None


def test_all_three_real_gate_names_are_named() -> None:
    assert NAMED_EXECUTION_GATES == {"idempotency", "confidence", "risk_floor"}


def test_denial_gate_is_a_plain_str_so_an_unknown_future_gate_still_parses() -> None:
    denial = parse_denial({"error": {"code": "execution_blocked_new_gate", "gate": "some_future_gate", "message": "?"}})
    assert denial is not None
    assert denial.gate == "some_future_gate"
    assert denial.gate not in NAMED_EXECUTION_GATES
