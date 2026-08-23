"""Unit tests for `pydantic_ai_algenta.receipts` -- no network, no agent, just the model."""

from __future__ import annotations

from pydantic_ai_algenta.receipts import GovernedExecutionReceipt, parse_receipt

FULL_ENVELOPE = {
    "status": "ok",
    "code": "ok",
    "retryable": False,
    "request_id": "req-1",
    "trace_id": "trace-1",
    "policy_snapshot_hash": "snap-1",
    "receipt_version": 1,
    "plan_hash": "plan-abc",
    "approval_state": "approved",
    "execution_id": "exec-1",
    "idempotency_key": "idem-1",
    "result": {"executed": True},
}


def test_receipt_round_trips_every_documented_field() -> None:
    receipt = GovernedExecutionReceipt.model_validate(FULL_ENVELOPE)
    assert receipt.status == "ok"
    assert receipt.code == "ok"
    assert receipt.retryable is False
    assert receipt.request_id == "req-1"
    assert receipt.trace_id == "trace-1"
    assert receipt.policy_snapshot_hash == "snap-1"
    assert receipt.receipt_version == 1
    assert receipt.plan_hash == "plan-abc"
    assert receipt.approval_state == "approved"
    assert receipt.execution_id == "exec-1"
    assert receipt.idempotency_key == "idem-1"
    assert receipt.result == {"executed": True}
    # And it dumps back to exactly the same shape it validated from.
    assert receipt.model_dump() == FULL_ENVELOPE


def test_receipt_tolerates_unknown_future_fields() -> None:
    envelope = dict(FULL_ENVELOPE, engine_build="2026.08.1")
    receipt = GovernedExecutionReceipt.model_validate(envelope)
    assert receipt.model_dump()["engine_build"] == "2026.08.1"


def test_parse_receipt_returns_none_for_non_envelope_dict() -> None:
    assert parse_receipt({"capabilities": ["query"], "engine_version": "1.4.0"}) is None


def test_parse_receipt_returns_none_for_non_dict() -> None:
    assert parse_receipt("plain string result") is None
    assert parse_receipt(None) is None
    assert parse_receipt(["a", "list"]) is None


def test_parse_receipt_returns_typed_object_for_a_real_envelope() -> None:
    receipt = parse_receipt(FULL_ENVELOPE)
    assert isinstance(receipt, GovernedExecutionReceipt)
    assert receipt.execution_id == "exec-1"


def test_parse_receipt_honors_a_custom_receipt_model() -> None:
    class CustomReceipt(GovernedExecutionReceipt):
        def shout_code(self) -> str:
            return self.code.upper()

    receipt = parse_receipt(FULL_ENVELOPE, model=CustomReceipt)
    assert isinstance(receipt, CustomReceipt)
    assert receipt.shout_code() == "OK"


def test_is_pending_approval() -> None:
    pending = GovernedExecutionReceipt.model_validate(dict(FULL_ENVELOPE, approval_state="pending"))
    assert pending.is_pending_approval()
    assert not pending.is_denied()
    assert not pending.is_success()


def test_is_denied_via_approval_state_rejected() -> None:
    rejected = GovernedExecutionReceipt.model_validate(
        dict(FULL_ENVELOPE, approval_state="rejected", status="error", code="policy_rejected")
    )
    assert rejected.is_denied()
    assert not rejected.is_pending_approval()
    assert not rejected.is_success()
    assert "the decision plan was rejected by policy" in rejected.denial_reason()


def test_is_denied_via_named_policy_gate_code_even_without_rejected_state() -> None:
    # A named 409-style gate can fire with approval_state left at "none" (e.g. it never got as
    # far as an approval decision -- the plan_hash itself didn't match).
    receipt = GovernedExecutionReceipt.model_validate(
        dict(FULL_ENVELOPE, approval_state="none", status="error", code="plan_hash_mismatch")
    )
    assert receipt.is_denied()


def test_is_denied_via_expired_approval() -> None:
    expired = GovernedExecutionReceipt.model_validate(dict(FULL_ENVELOPE, approval_state="expired", status="error"))
    assert expired.is_denied()
    assert "approval window" in expired.denial_reason()


def test_is_success_true_for_none_or_approved_with_ok_status() -> None:
    none_state = GovernedExecutionReceipt.model_validate(dict(FULL_ENVELOPE, approval_state="none"))
    approved_state = GovernedExecutionReceipt.model_validate(dict(FULL_ENVELOPE, approval_state="approved"))
    assert none_state.is_success()
    assert approved_state.is_success()


def test_is_success_false_when_status_is_error_even_without_a_named_gate() -> None:
    # A generic upstream failure: not a policy denial, not pending -- just failed.
    receipt = GovernedExecutionReceipt.model_validate(
        dict(FULL_ENVELOPE, approval_state="none", status="error", code="upstream_timeout")
    )
    assert not receipt.is_success()
    assert not receipt.is_denied()
    assert not receipt.is_pending_approval()


def test_denial_reason_prefers_a_message_embedded_in_result() -> None:
    receipt = GovernedExecutionReceipt.model_validate(
        dict(
            FULL_ENVELOPE,
            approval_state="rejected",
            code="plan_hash_mismatch",
            result={"message": "the plan changed after this call was issued"},
        )
    )
    assert receipt.denial_reason() == "plan_hash_mismatch: the plan changed after this call was issued"
