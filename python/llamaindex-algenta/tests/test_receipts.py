"""Unit tests for `llamaindex_algenta.receipts` -- no MCP wire involved, hand-built fakes only.

`test_toolset_scenarios.py` exercises the same parsing over the real wire; these tests pin down
the parsing/unwrap logic itself in isolation, including the shapes real `mcp.types.CallToolResult`
takes that a hand-built dict fake can reproduce faithfully (`structuredContent`, `content`,
`isError`).
"""

from __future__ import annotations

from typing import Any

from llamaindex_algenta.receipts import (
    ExecutionDenial,
    ExecutionReceipt,
    call_error_text,
    is_call_error,
    parse_execution_denial,
    parse_execution_receipt,
    unwrap_call_tool_result,
)
from pydantic import ValidationError


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeCallToolResult:
    def __init__(self, *, content: list[Any] | None = None, structured_content: dict | None = None, is_error: bool = False) -> None:
        self.content = content or []
        self.structuredContent = structured_content
        self.isError = is_error


def test_parse_execution_receipt_accepts_a_well_formed_dict() -> None:
    receipt = parse_execution_receipt(
        {"decision_id": "d1", "webhook_url": "https://example.com/hook", "execution_status": "delivered"}
    )
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.decision_id == "d1"
    assert receipt.safety_overridden is False


def test_parse_execution_receipt_returns_none_for_a_non_envelope_dict() -> None:
    assert parse_execution_receipt({"capabilities": ["query"], "engine_version": "1.0"}) is None


def test_parse_execution_receipt_returns_none_for_a_denial_shaped_dict() -> None:
    assert (
        parse_execution_receipt(
            {"error": {"code": "execution_blocked_confidence", "gate": "confidence", "message": "too risky"}}
        )
        is None
    )


def test_parse_execution_receipt_returns_none_for_a_non_dict() -> None:
    assert parse_execution_receipt("just a string") is None
    assert parse_execution_receipt(None) is None
    assert parse_execution_receipt([1, 2, 3]) is None


def test_parse_execution_denial_accepts_the_real_409_body_shape() -> None:
    denial = parse_execution_denial(
        {
            "error": {
                "code": "execution_blocked_idempotency",
                "gate": "idempotency",
                "message": "already delivered",
                "override_hint": "Retry with force=true.",
            }
        }
    )
    assert isinstance(denial, ExecutionDenial)
    assert denial.gate == "idempotency"
    assert denial.code == "execution_blocked_idempotency"
    assert denial.override_hint == "Retry with force=true."


def test_parse_execution_denial_returns_none_without_an_error_key() -> None:
    assert parse_execution_denial({"decision_id": "d1", "webhook_url": "h", "execution_status": "delivered"}) is None


def test_parse_execution_denial_returns_none_for_a_non_dict() -> None:
    assert parse_execution_denial("just a string") is None
    assert parse_execution_denial(None) is None


def test_execution_denial_rejects_an_unknown_gate_name() -> None:
    # Only "idempotency" | "confidence" | "risk_floor" are real gate names -- anything else is
    # not a real execution-blocked denial and must fail validation, not be silently accepted.
    try:
        ExecutionDenial.model_validate({"code": "execution_blocked_bogus", "gate": "bogus", "message": "nope"})
    except ValidationError:
        pass
    else:
        raise AssertionError("expected ValidationError for an unknown gate name")


def test_unwrap_prefers_structured_content_over_text_json() -> None:
    raw = _FakeCallToolResult(
        structured_content={"decision_id": "d1", "webhook_url": "h", "execution_status": "delivered", "source": "structured"},
        content=[_FakeTextContent('{"decision_id": "d1", "webhook_url": "h", "execution_status": "delivered", "source": "text"}')],
    )
    payload = unwrap_call_tool_result(raw)
    assert payload["source"] == "structured"


def test_unwrap_falls_back_to_text_content_json_when_no_structured_content() -> None:
    raw = _FakeCallToolResult(content=[_FakeTextContent('{"decision_id": "d1", "webhook_url": "h", "execution_status": "delivered"}')])
    payload = unwrap_call_tool_result(raw)
    assert payload == {"decision_id": "d1", "webhook_url": "h", "execution_status": "delivered"}


def test_unwrap_returns_none_for_unparseable_text_content() -> None:
    raw = _FakeCallToolResult(content=[_FakeTextContent("not json at all")])
    assert unwrap_call_tool_result(raw) is None


def test_unwrap_accepts_a_plain_already_unwrapped_dict() -> None:
    assert unwrap_call_tool_result({"decision_id": "d1"}) == {"decision_id": "d1"}


def test_unwrap_accepts_a_json_string_defensively() -> None:
    assert unwrap_call_tool_result('{"decision_id": "d1"}') == {"decision_id": "d1"}


def test_unwrap_returns_none_for_a_non_json_string() -> None:
    assert unwrap_call_tool_result("not json") is None


def test_is_call_error_reflects_the_real_isError_flag() -> None:
    assert is_call_error(_FakeCallToolResult(is_error=True)) is True
    assert is_call_error(_FakeCallToolResult(is_error=False)) is False
    assert is_call_error({"isError": True}) is False  # duck-typed via getattr, not dict lookup
    assert is_call_error("not a call result") is False


def test_call_error_text_joins_text_content_blocks() -> None:
    raw = _FakeCallToolResult(content=[_FakeTextContent("Error executing tool blow_up: boom")], is_error=True)
    assert "boom" in call_error_text(raw)


def test_call_error_text_falls_back_to_str_when_no_text_content() -> None:
    raw = _FakeCallToolResult(is_error=True)
    assert call_error_text(raw) == str(raw)


def test_execution_receipt_round_trips_through_model_dump() -> None:
    receipt = ExecutionReceipt(decision_id="d1", webhook_url="https://example.com/hook", execution_status="delivered")
    assert parse_execution_receipt(receipt.model_dump()) == receipt


def test_execution_receipt_preserves_unknown_future_fields() -> None:
    receipt = parse_execution_receipt(
        {"decision_id": "d1", "webhook_url": "h", "execution_status": "delivered", "future_field": "surprise"}
    )
    assert receipt is not None
    assert receipt.model_dump()["future_field"] == "surprise"
