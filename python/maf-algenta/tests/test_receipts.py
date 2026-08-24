"""Unit tests for `maf_algenta.receipts` -- no network, no agent, just the models."""

from __future__ import annotations

from maf_algenta.receipts import (
    ExecutionBlocked,
    ExecutionReceipt,
    parse_execution_blocked,
    parse_execution_receipt,
)

FULL_RECEIPT = {
    "decision_id": "decision-1",
    "webhook_url": "https://example.com/hook",
    "execution_status": "delivered",
    "response_code": 200,
    "executed_at": "2026-08-23T00:00:00+00:00",
    "policy_snapshot_id": "policy-snap-1",
    "schema_snapshot_id": "schema-snap-1",
    "manifest_version": "1",
    "payload_summary": {"note": "ok"},
    "safety_overridden": False,
}

FULL_BLOCKED_ERROR_BODY = {
    "error": {
        "code": "execution_blocked_confidence",
        "gate": "confidence",
        "message": "decision confidence 0.41 is below policy.min_confidence 0.60.",
        "override_hint": "Set override_safety=true to bypass the confidence gate.",
    }
}


def test_receipt_round_trips_every_documented_field() -> None:
    receipt = ExecutionReceipt.model_validate(FULL_RECEIPT)
    assert receipt.decision_id == "decision-1"
    assert receipt.webhook_url == "https://example.com/hook"
    assert receipt.execution_status == "delivered"
    assert receipt.response_code == 200
    assert receipt.executed_at == "2026-08-23T00:00:00+00:00"
    assert receipt.policy_snapshot_id == "policy-snap-1"
    assert receipt.schema_snapshot_id == "schema-snap-1"
    assert receipt.manifest_version == "1"
    assert receipt.payload_summary == {"note": "ok"}
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


def test_is_delivered_false_for_failed_status() -> None:
    # A "failed" webhook delivery is still a real, successful `execute_decision` call -- no
    # exception, just a receipt reporting the delivery itself didn't go through.
    receipt = ExecutionReceipt.model_validate(dict(FULL_RECEIPT, execution_status="failed", response_code=503))
    assert not receipt.is_delivered()
    assert receipt.response_code == 503


def test_parse_execution_receipt_returns_none_for_non_receipt_dict() -> None:
    assert parse_execution_receipt({"capabilities": ["query"], "engine_version": "1.4.0"}) is None


def test_parse_execution_receipt_returns_none_for_non_dict() -> None:
    assert parse_execution_receipt("plain string result") is None
    assert parse_execution_receipt(None) is None
    assert parse_execution_receipt(["a", "list"]) is None


def test_parse_execution_receipt_returns_typed_object_for_a_real_receipt() -> None:
    receipt = parse_execution_receipt(FULL_RECEIPT)
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.decision_id == "decision-1"


def test_parse_execution_receipt_honors_a_custom_receipt_model() -> None:
    class CustomReceipt(ExecutionReceipt):
        def shout_status(self) -> str:
            return self.execution_status.upper()

    receipt = parse_execution_receipt(FULL_RECEIPT, model=CustomReceipt)
    assert isinstance(receipt, CustomReceipt)
    assert receipt.shout_status() == "DELIVERED"


def test_parse_execution_blocked_returns_typed_object_for_a_real_denial_body() -> None:
    blocked = parse_execution_blocked(FULL_BLOCKED_ERROR_BODY)
    assert isinstance(blocked, ExecutionBlocked)
    assert blocked.code == "execution_blocked_confidence"
    assert blocked.gate == "confidence"
    assert "0.41" in blocked.message
    assert blocked.override_hint == "Set override_safety=true to bypass the confidence gate."


def test_parse_execution_blocked_returns_none_without_an_error_key() -> None:
    assert parse_execution_blocked({"capabilities": ["query"]}) is None


def test_parse_execution_blocked_returns_none_for_non_dict() -> None:
    assert parse_execution_blocked("plain string") is None
    assert parse_execution_blocked(None) is None


def test_parse_execution_blocked_returns_none_when_error_is_missing_required_fields() -> None:
    assert parse_execution_blocked({"error": {"code": "execution_blocked_confidence"}}) is None


def test_execution_blocked_accepts_an_unknown_future_gate_name() -> None:
    # `gate` is plain `str`, not the `ExecutionGate` literal, on purpose: a gate name this
    # package doesn't know about yet still parses as a real denial.
    body = {
        "error": {
            "code": "execution_blocked_new_gate",
            "gate": "some_future_gate",
            "message": "blocked by a gate this package has never heard of",
            "override_hint": None,
        }
    }
    blocked = parse_execution_blocked(body)
    assert isinstance(blocked, ExecutionBlocked)
    assert blocked.gate == "some_future_gate"
