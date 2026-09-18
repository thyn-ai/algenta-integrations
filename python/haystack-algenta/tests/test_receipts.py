"""Unit tests for `haystack_algenta.receipts` -- no network, no agent, just the models and the
double-JSON unwrap.

Shapes below are the real `execute_decision` contract: a 200 `ExecutionReceipt`
(`decision_id`/`webhook_url`/`execution_status`/...) or a 409 `{"error": {...}}` naming exactly one
of the three real gates (`ExecutionBlocked`). Neither carries `plan_hash`/`approval_state`/
`receipt_version`/`retryable` -- those never existed on the real tool.
"""

from __future__ import annotations

import json

import pytest
from haystack_algenta.receipts import (
    ExecutionBlocked,
    ExecutionReceipt,
    extract_execution_outcome_from_tool_result,
    parse_execution_outcome,
    unwrap_mcp_tool_result,
)
from pydantic import ValidationError

FULL_RECEIPT = {
    "decision_id": "decision-1",
    "webhook_url": "https://example.test/hook",
    "execution_status": "delivered",
    "response_code": 200,
    "executed_at": "2026-08-23T00:00:00Z",
    "policy_snapshot_id": "policy-1",
    "schema_snapshot_id": "schema-1",
    "manifest_version": "1",
    "payload_summary": {"chosen_action": "hold"},
    "safety_overridden": False,
}

CONFIDENCE_BLOCKED = {
    "error": {
        "code": "execution_blocked_confidence",
        "gate": "confidence",
        "message": "decision confidence 0.32 is below policy.min_confidence 0.60",
        "override_hint": "Set override_safety=true to bypass the confidence gate.",
    }
}


def test_receipt_round_trips_every_documented_field() -> None:
    receipt = ExecutionReceipt.model_validate(FULL_RECEIPT)
    assert receipt.decision_id == "decision-1"
    assert receipt.webhook_url == "https://example.test/hook"
    assert receipt.execution_status == "delivered"
    assert receipt.response_code == 200
    assert receipt.executed_at == "2026-08-23T00:00:00Z"
    assert receipt.policy_snapshot_id == "policy-1"
    assert receipt.schema_snapshot_id == "schema-1"
    assert receipt.manifest_version == "1"
    assert receipt.payload_summary == {"chosen_action": "hold"}
    assert receipt.safety_overridden is False
    assert receipt.model_dump() == FULL_RECEIPT
    assert receipt.is_delivered()


def test_receipt_tolerates_unknown_future_fields() -> None:
    envelope = dict(FULL_RECEIPT, engine_build="2026.08.1")
    receipt = ExecutionReceipt.model_validate(envelope)
    assert receipt.model_dump()["engine_build"] == "2026.08.1"


def test_receipt_execution_status_failed_is_not_delivered_but_still_a_valid_receipt() -> None:
    envelope = dict(FULL_RECEIPT, execution_status="failed", response_code=None)
    receipt = ExecutionReceipt.model_validate(envelope)
    assert not receipt.is_delivered()


def test_execution_blocked_round_trips_the_real_denial_fields() -> None:
    blocked = ExecutionBlocked.model_validate(CONFIDENCE_BLOCKED["error"])
    assert blocked.code == "execution_blocked_confidence"
    assert blocked.gate == "confidence"
    assert "0.32" in blocked.message
    assert blocked.override_hint == "Set override_safety=true to bypass the confidence gate."


def test_execution_blocked_rejects_an_unknown_gate_name() -> None:
    with pytest.raises(ValidationError):
        ExecutionBlocked.model_validate({"code": "execution_blocked_mystery", "gate": "mystery", "message": "?"})


def test_parse_execution_outcome_returns_receipt_for_a_real_success_body() -> None:
    outcome = parse_execution_outcome(FULL_RECEIPT)
    assert isinstance(outcome, ExecutionReceipt)
    assert outcome.decision_id == "decision-1"


def test_parse_execution_outcome_returns_blocked_for_each_real_named_gate() -> None:
    for gate in ("idempotency", "confidence", "risk_floor"):
        body = {
            "error": {
                "code": f"execution_blocked_{gate}",
                "gate": gate,
                "message": f"blocked by {gate}",
                "override_hint": "see docs",
            }
        }
        outcome = parse_execution_outcome(body)
        assert isinstance(outcome, ExecutionBlocked)
        assert outcome.gate == gate


def test_parse_execution_outcome_returns_none_for_a_non_execute_decision_result() -> None:
    # e.g. get_contract's discovery payload, or log_decision's own (unrelated) result shape.
    assert parse_execution_outcome({"capabilities": ["query"], "engine_version": "1.4.0"}) is None
    assert parse_execution_outcome({"decision_id": "d-1", "chosen_action": "hold", "note": None}) is None


def test_parse_execution_outcome_returns_none_for_non_dict() -> None:
    assert parse_execution_outcome("plain string result") is None
    assert parse_execution_outcome(None) is None
    assert parse_execution_outcome(["a", "list"]) is None


def test_parse_execution_outcome_returns_none_for_an_error_key_that_doesnt_validate() -> None:
    # An "error" dict present but missing the required real fields shouldn't fall through to a
    # (guaranteed-to-fail) ExecutionReceipt attempt -- it's just unparseable.
    assert parse_execution_outcome({"error": {"message": "no code or gate here"}}) is None


def test_parse_execution_outcome_honors_custom_models() -> None:
    class CustomReceipt(ExecutionReceipt):
        def shout_status(self) -> str:
            return self.execution_status.upper()

    outcome = parse_execution_outcome(FULL_RECEIPT, receipt_model=CustomReceipt)
    assert isinstance(outcome, CustomReceipt)
    assert outcome.shout_status() == "DELIVERED"


# --- unwrap_mcp_tool_result / extract_execution_outcome_from_tool_result -------------------------


def _double_json_envelope(payload: dict) -> str:
    """Build the exact double-JSON-string shape a real `Tool.invoke()` call against an
    `MCPToolset`-built tool returns (verified live -- see the package README)."""
    return json.dumps(
        {
            "meta": None,
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "structuredContent": None,
            "isError": False,
        }
    )


def test_unwrap_peels_the_real_double_json_mcp_envelope() -> None:
    raw = _double_json_envelope(FULL_RECEIPT)
    assert unwrap_mcp_tool_result(raw) == FULL_RECEIPT


def test_unwrap_prefers_structured_content_when_present() -> None:
    raw = json.dumps(
        {
            "meta": None,
            "content": [{"type": "text", "text": json.dumps(FULL_RECEIPT)}],
            "structuredContent": {"decision_id": "shortcut", "shortcut": True},
            "isError": False,
        }
    )
    assert unwrap_mcp_tool_result(raw) == {"decision_id": "shortcut", "shortcut": True}


def test_unwrap_accepts_a_plain_dict_with_no_mcp_envelope_at_all() -> None:
    # The `tools=`/`mcp_toolset=` escape hatches' fake tools may just return a dict directly.
    assert unwrap_mcp_tool_result(dict(FULL_RECEIPT)) == FULL_RECEIPT


def test_unwrap_returns_none_for_unparseable_input() -> None:
    assert unwrap_mcp_tool_result("not json at all") is None
    assert unwrap_mcp_tool_result(None) is None
    assert unwrap_mcp_tool_result(json.dumps({"content": [{"type": "text", "text": "not json either"}]})) is None


def test_extract_execution_outcome_end_to_end_through_the_double_json_envelope() -> None:
    raw = _double_json_envelope(FULL_RECEIPT)
    outcome = extract_execution_outcome_from_tool_result(raw)
    assert isinstance(outcome, ExecutionReceipt)
    assert outcome.decision_id == "decision-1"


def test_extract_execution_outcome_end_to_end_for_a_real_denial() -> None:
    raw = _double_json_envelope(CONFIDENCE_BLOCKED)
    outcome = extract_execution_outcome_from_tool_result(raw)
    assert isinstance(outcome, ExecutionBlocked)
    assert outcome.gate == "confidence"


def test_extract_execution_outcome_passes_through_a_non_execute_decision_result() -> None:
    non_envelope = {"capabilities": ["query"], "engine_version": "1.4.0"}
    raw = _double_json_envelope(non_envelope)
    assert extract_execution_outcome_from_tool_result(raw) is None
