"""Edge-case coverage for `google_adk_algenta.toolset` helpers and escape hatches."""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.tools import BaseTool, FunctionTool
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google_adk_algenta import (
    AlgentaExecutionBlocked,
    AlgentaToolDenied,
    AlgentaToolset,
    ExecutionReceipt,
)
from google_adk_algenta.toolset import (
    AlgentaBaseTool,
    AlgentaMcpTool,
    _extract_tool_payload,
    _strip_never_model_facing_schema,
)

from .helpers import bare_tool_context
from .stub_server import StubServerFixture


def test_strip_never_model_facing_schema_returns_non_dict_unchanged() -> None:
    assert _strip_never_model_facing_schema(None) is None
    assert _strip_never_model_facing_schema("schema") == "schema"


def test_extract_tool_payload_returns_non_dict_unchanged() -> None:
    assert _extract_tool_payload("plain") == "plain"


def test_extract_tool_payload_parses_text_content() -> None:
    raw = {"content": [{"type": "text", "text": '{"ok": true}'}]}
    assert _extract_tool_payload(raw) == {"ok": True}


def test_extract_tool_payload_returns_text_on_json_decode_error() -> None:
    raw = {"content": [{"type": "text", "text": "not json"}]}
    assert _extract_tool_payload(raw) == "not json"


def test_extract_tool_payload_skips_non_dict_content_blocks() -> None:
    raw = {"content": ["not a dict", {"type": "text", "text": '{"ok": true}'}]}
    assert _extract_tool_payload(raw) == {"ok": True}


@pytest.mark.anyio
async def test_base_tool_with_none_declaration() -> None:
    class ToolWithNoneDeclaration(BaseTool):
        def _get_declaration(self) -> Any:
            return None

        async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
            return {}

    tool = ToolWithNoneDeclaration(name="noop", description="noop")
    wrapped = AlgentaToolset(tools=[tool], profile="full")
    # Constructing and getting tools covers the None-declaration branch.
    tools = await wrapped.get_tools(bare_tool_context())
    assert tools[0]._get_declaration() is None


def test_toolset_rejects_multiple_sources() -> None:
    with pytest.raises(ValueError, match="exactly one of `base_url`, `wrapped`, or `tools`"):
        AlgentaToolset(base_url="http://localhost:8000/mcp", tools=[])


def test_toolset_accepts_wrapped_toolset() -> None:
    inner = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url="http://localhost:8000/mcp")
    )
    toolset = AlgentaToolset(wrapped=inner, profile="observe")
    assert toolset._inner is inner


@pytest.mark.anyio
async def test_toolset_close_closes_inner() -> None:
    inner = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url="http://localhost:8000/mcp")
    )
    toolset = AlgentaToolset(wrapped=inner)
    await toolset.close()


@pytest.mark.anyio
async def test_base_tool_profile_denial() -> None:
    def execute_decision(decision_id: str, webhook_url: str) -> dict:
        return {
            "decision_id": decision_id,
            "webhook_url": webhook_url,
            "execution_status": "delivered",
        }

    # Construct the wrapper with profile="observe" so the call-time check sees an
    # execute-tier tool name outside its allowed profile and raises AlgentaToolDenied.
    wrapped = AlgentaBaseTool(
        tool=FunctionTool(execute_decision), profile="observe", receipt_model=ExecutionReceipt
    )
    with pytest.raises(AlgentaToolDenied):
        await wrapped.run_async(
            args={"decision_id": "dec-1", "webhook_url": "https://example.com/hook"},
            tool_context=bare_tool_context(),
        )


@pytest.mark.anyio
async def test_base_tool_non_envelope_passthrough() -> None:
    def recommend(scenario: str) -> dict:
        return {"scenario": scenario, "recommended_action": "hold"}

    toolset = AlgentaToolset(tools=[FunctionTool(recommend)], profile="observe")
    tools = await toolset.get_tools(bare_tool_context())
    result = await tools[0].run_async(args={"scenario": "x"}, tool_context=bare_tool_context())
    assert result["recommended_action"] == "hold"


@pytest.mark.anyio
async def test_base_tool_denial_mapping() -> None:
    def execute_decision(decision_id: str, webhook_url: str) -> dict:
        return {
            "error": {
                "code": "execution_blocked_confidence",
                "gate": "confidence",
                "message": "blocked",
            }
        }

    toolset = AlgentaToolset(tools=[FunctionTool(execute_decision)], profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await tools[0].run_async(
            args={"decision_id": "dec-1", "webhook_url": "https://example.com/hook"},
            tool_context=bare_tool_context(),
        )
    assert exc_info.value.gate == "confidence"


@pytest.mark.anyio
async def test_mcp_tool_profile_denial() -> None:
    # Construct an AlgentaMcpTool for execute_decision but with profile="observe". The
    # call-time profile check should raise even though the tool object exists.
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    async with StubServerFixture() as server:
        toolset = McpToolset(connection_params=StreamableHTTPConnectionParams(url=server.base_url))
        tools = await toolset.get_tools()
        execute = next(tool for tool in tools if tool.name == "execute_decision")
        wrapped = AlgentaMcpTool(
            profile="observe",
            receipt_model=ExecutionReceipt,
            mcp_tool=execute.raw_mcp_tool,
            mcp_session_manager=execute._mcp_session_manager,
        )
        # Exercise the declaration path (which strips never-model-facing fields from the schema).
        declaration = wrapped._get_declaration()
        assert "force" not in declaration.parameters_json_schema.get("properties", {})
        assert "override_safety" not in declaration.parameters_json_schema.get("properties", {})

        with pytest.raises(AlgentaToolDenied):
            await wrapped.run_async(
                args={"decision_id": "dec-1", "webhook_url": "https://example.com/hook"},
                tool_context=bare_tool_context(),
            )
        await toolset.close()


def test_version_fallback_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata
    import sys

    real_version = importlib.metadata.version

    def fake_version(name: str) -> str:
        if name == "google-adk-algenta":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_version(name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)

    # Remove cached module so the import re-runs the try/except block.
    for mod_name in list(sys.modules):
        if mod_name == "google_adk_algenta" or mod_name.startswith("google_adk_algenta."):
            del sys.modules[mod_name]

    import google_adk_algenta

    assert google_adk_algenta.__version__ == "0+unknown"
