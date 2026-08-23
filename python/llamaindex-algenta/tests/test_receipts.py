"""Unit tests for `llamaindex_algenta.receipts` -- no MCP wire involved, hand-built fakes only.

`test_toolset_scenarios.py` exercises the same parsing over the real wire; these tests pin down
the parsing/unwrap logic itself in isolation, including the shapes real `mcp.types.CallToolResult`
takes that a hand-built dict fake can reproduce faithfully (`structuredContent`, `content`,
`isError`).
"""

from __future__ import annotations

from typing import Any

from llamaindex_algenta.receipts import (
    GovernedExecutionReceipt,
    call_error_text,
    is_call_error,
    parse_receipt,
    unwrap_call_tool_result,
)


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeCallToolResult:
    def __init__(self, *, content: list[Any] | None = None, structured_content: dict | None = None, is_error: bool = False) -> None:
        self.content = content or []
        self.structuredContent = structured_content
        self.isError = is_error


def test_parse_receipt_accepts_a_well_formed_dict() -> None:
    receipt = parse_receipt({"status": "ok", "code": "ok", "result": {"a": 1}})
    assert isinstance(receipt, GovernedExecutionReceipt)
    assert receipt.is_success()


def test_parse_receipt_returns_none_for_a_non_envelope_dict() -> None:
    assert parse_receipt({"capabilities": ["query"], "engine_version": "1.0"}) is None


def test_parse_receipt_returns_none_for_a_non_dict() -> None:
    assert parse_receipt("just a string") is None
    assert parse_receipt(None) is None
    assert parse_receipt([1, 2, 3]) is None


def test_unwrap_prefers_structured_content_over_text_json() -> None:
    raw = _FakeCallToolResult(
        structured_content={"status": "ok", "code": "ok", "result": {"from": "structured"}},
        content=[_FakeTextContent('{"status": "ok", "code": "ok", "result": {"from": "text"}}')],
    )
    payload = unwrap_call_tool_result(raw)
    assert payload["result"] == {"from": "structured"}


def test_unwrap_falls_back_to_text_content_json_when_no_structured_content() -> None:
    raw = _FakeCallToolResult(content=[_FakeTextContent('{"status": "ok", "code": "ok", "result": 42}')])
    payload = unwrap_call_tool_result(raw)
    assert payload == {"status": "ok", "code": "ok", "result": 42}


def test_unwrap_returns_none_for_unparseable_text_content() -> None:
    raw = _FakeCallToolResult(content=[_FakeTextContent("not json at all")])
    assert unwrap_call_tool_result(raw) is None


def test_unwrap_accepts_a_plain_already_unwrapped_dict() -> None:
    assert unwrap_call_tool_result({"status": "ok", "code": "ok"}) == {"status": "ok", "code": "ok"}


def test_unwrap_accepts_a_json_string_defensively() -> None:
    assert unwrap_call_tool_result('{"status": "ok", "code": "ok"}') == {"status": "ok", "code": "ok"}


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


def test_receipt_is_denied_for_named_policy_gate_code() -> None:
    receipt = GovernedExecutionReceipt(status="error", code="plan_hash_mismatch", approval_state="approved")
    assert receipt.is_denied()
    assert not receipt.is_success()


def test_receipt_denial_reason_prefers_result_message() -> None:
    receipt = GovernedExecutionReceipt(
        status="error", code="stale_plan", approval_state="rejected", result={"message": "plan expired at the engine"}
    )
    assert receipt.denial_reason() == "stale_plan: plan expired at the engine"


def test_receipt_round_trips_through_model_dump() -> None:
    receipt = GovernedExecutionReceipt(status="ok", code="ok", plan_hash="p1", result={"x": 1})
    assert parse_receipt(receipt.model_dump()) == receipt


def test_receipt_preserves_unknown_future_fields() -> None:
    receipt = parse_receipt({"status": "ok", "code": "ok", "future_field": "surprise"})
    assert receipt is not None
    assert receipt.model_dump()["future_field"] == "surprise"
