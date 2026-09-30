"""Edge-case and error-path coverage for `smolagents_algenta.toolset`.

These tests do not need the stub MCP server; they exercise pure logic and the internal
`_BackgroundMCPClient` helper directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from smolagents import Tool
from smolagents_algenta import AlgentaToolset, ExecutionReceipt
from smolagents_algenta.toolset import (
    _BackgroundMCPClient,
    _extract_tool_result,
    _mcp_schema_to_inputs,
)


class _ExecuteDecision(Tool):
    name = "execute_decision"
    description = "Execute."
    inputs = {
        "decision_id": {"type": "string", "description": "id"},
        "webhook_url": {"type": "string", "description": "url"},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, decision_id: str, webhook_url: str) -> dict:
        return {"decision_id": decision_id, "webhook_url": webhook_url, "execution_status": "delivered"}


def test_both_wrapped_and_base_url_rejected() -> None:
    with pytest.raises(ValueError, match="Pass either `wrapped` or `base_url`"):
        AlgentaToolset(wrapped=[_ExecuteDecision()], base_url="http://localhost:8000/mcp")


def test_mcp_schema_with_list_type_marks_nullable() -> None:
    schema = {
        "type": "object",
        "properties": {
            "dataset": {"type": ["string", "null"], "description": "dataset"},
            "count": {"type": "integer", "description": "count"},
        },
        "required": ["count"],
    }
    inputs = _mcp_schema_to_inputs(schema)
    assert inputs["dataset"]["type"] == "string"
    assert inputs["dataset"]["nullable"] is True
    assert inputs["count"]["type"] == "integer"
    assert "nullable" not in inputs["count"]


def test_mcp_schema_with_empty_property_uses_name_as_description() -> None:
    schema = {"type": "object", "properties": {"dataset": {}}}
    inputs = _mcp_schema_to_inputs(schema)
    assert inputs["dataset"]["description"] == "dataset"


def test_extract_tool_result_prefers_structured_content() -> None:
    result = MagicMock()
    result.is_error = False
    result.structured_content = {"decision_id": "dec-1"}
    result.content = []
    assert _extract_tool_result(result) == {"decision_id": "dec-1"}


def test_extract_tool_result_parses_single_text_json() -> None:
    content = MagicMock()
    content.type = "text"
    content.text = '{"ok": true}'
    result = MagicMock()
    result.is_error = False
    result.structured_content = None
    result.content = [content]
    assert _extract_tool_result(result) == {"ok": True}


def test_extract_tool_result_returns_plain_text_for_non_json() -> None:
    content = MagicMock()
    content.type = "text"
    content.text = "plain text"
    result = MagicMock()
    result.is_error = False
    result.structured_content = None
    result.content = [content]
    assert _extract_tool_result(result) == "plain text"


def test_extract_tool_result_dumps_multiple_content_items() -> None:
    content = MagicMock()
    content.type = "image"
    content.model_dump.return_value = {"type": "image", "data": "..."}
    result = MagicMock()
    result.is_error = False
    result.structured_content = None
    result.content = [content, content]
    assert _extract_tool_result(result) == [{"type": "image", "data": "..."}, {"type": "image", "data": "..."}]


def test_extract_tool_result_returns_raw_when_no_content() -> None:
    result = {"not_a_call_tool_result": True}
    assert _extract_tool_result(result) == result


def test_extract_tool_result_raises_on_error_result() -> None:
    from smolagents import AgentToolExecutionError

    content = MagicMock()
    content.type = "text"
    content.text = "error message"
    result = MagicMock()
    result.is_error = True
    result.content = [content]
    with pytest.raises(AgentToolExecutionError, match="error message"):
        _extract_tool_result(result)


def test_background_client_connect_is_idempotent() -> None:
    client = _BackgroundMCPClient("http://localhost:8000/mcp")
    # Simulate an already-started client to avoid real network.
    client._thread = MagicMock()
    client._loop = MagicMock()
    client.connect()
    assert client._client is None  # early return means no Client created


def test_background_client_list_tools_before_connect_raises() -> None:
    client = _BackgroundMCPClient("http://localhost:8000/mcp")
    with pytest.raises(RuntimeError, match="MCP client is not connected"):
        client.list_tools()


def test_background_client_call_tool_before_connect_raises() -> None:
    client = _BackgroundMCPClient("http://localhost:8000/mcp")
    with pytest.raises(RuntimeError, match="MCP client is not connected"):
        client.call_tool("execute_decision", {})


def test_background_client_run_without_loop_raises() -> None:
    client = _BackgroundMCPClient("http://localhost:8000/mcp")
    client._client = MagicMock()
    client._loop = None
    with pytest.raises(RuntimeError, match="Background MCP client loop is not running"):
        client.list_tools()


def test_background_client_connect_failure_when_loop_does_not_start() -> None:
    client = _BackgroundMCPClient("http://localhost:8000/mcp")
    with patch("threading.Event.wait", return_value=None):
        with pytest.raises(RuntimeError, match="Background MCP client loop failed to start"):
            client.connect()


def test_receipt_model_override_used_for_wrapped_execute_decision() -> None:
    class LoudReceipt(ExecutionReceipt):
        def status(self) -> str:
            return self.execution_status.upper()

    with AlgentaToolset(wrapped=[_ExecuteDecision()], profile="execute", receipt_model=LoudReceipt) as toolset:
        tool = toolset.tools[0]
        result = tool.forward(decision_id="dec-1", webhook_url="https://example.com/hook")
        assert isinstance(result, LoudReceipt)
        assert result.status() == "DELIVERED"


def test_never_model_facing_input_fields_are_scrubbed_from_mcp_schema() -> None:
    schema = {
        "type": "object",
        "properties": {
            "decision_id": {"type": "string", "description": "id"},
            "force": {"type": "boolean", "description": "force"},
            "override_safety": {"type": "boolean", "description": "override"},
        },
        "required": ["decision_id"],
    }
    inputs = _mcp_schema_to_inputs(schema)
    assert set(inputs) == {"decision_id"}
