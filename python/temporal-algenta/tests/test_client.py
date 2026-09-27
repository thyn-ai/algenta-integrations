"""Tests for `temporal_algenta.client.AlgentaMcpClient` against the real stub-engine wire:
profile enforcement, payload extraction, denial mapping, and idempotency behavior. No mocks of
the client internals -- every test goes through a real MCP round trip.

Wire calls go through `tests.helpers.with_session_resilient`, which retries only session-level
`McpError`s (a known, intermittent streamable-HTTP session-establishment race in the MCP SDK
-- never governed-call outcomes; see `tests/helpers.py`).
"""

from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent
from temporal_algenta.client import (
    DEFAULT_ALGENTA_BASE_URL,
    AlgentaMcpClient,
    extract_call_tool_payload,
)
from temporal_algenta.errors import (
    AlgentaExecutionBlocked,
    AlgentaToolCallFailed,
    AlgentaToolDenied,
)

from .helpers import with_session_resilient
from .stub_server import (
    BELOW_RISK_FLOOR_DECISION_ID,
    FLAKY_DATASET,
    LOW_CONFIDENCE_DECISION_ID,
)


async def test_list_tool_names_returns_the_advertised_registry(stub_server) -> None:
    base_url, _engine = stub_server
    names = await with_session_resilient(base_url=base_url, profile="full", fn=lambda c: c.list_tool_names())
    assert names == frozenset(
        {
            "get_contract",
            "query_data",
            "simulate",
            "recommend",
            "plan_decision",
            "log_decision",
            "execute_decision",
            "_test_diagnostics",
        }
    )


async def test_call_tool_returns_unwrapped_dict_payload(stub_server) -> None:
    base_url, _engine = stub_server
    result = await with_session_resilient(
        base_url=base_url, profile="observe", fn=lambda c: c.call_tool("simulate", {"scenario": "spx"})
    )
    assert result["scenario"] == "spx"
    assert "expected_value" in result


async def test_call_tool_enforces_profile_before_any_call(stub_server) -> None:
    base_url, engine = stub_server
    with pytest.raises(AlgentaToolDenied):
        await with_session_resilient(
            base_url=base_url,
            profile="observe",
            fn=lambda c: c.call_tool("execute_decision", {"decision_id": "decision-x", "webhook_url": "http://x"}),
        )
    # The refusal happened client-side: the engine never saw an execute attempt.
    assert engine.execute_attempts == {}


async def test_full_profile_allows_tools_outside_the_named_contract(stub_server) -> None:
    base_url, _engine = stub_server
    result = await with_session_resilient(base_url=base_url, profile="full", fn=lambda c: c.call_tool("_test_diagnostics"))
    assert result == {"delivered_count": 0}


async def test_denial_raises_execution_blocked_with_parsed_denial(stub_server) -> None:
    base_url, _engine = stub_server
    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await with_session_resilient(
            base_url=base_url,
            profile="execute",
            fn=lambda c: c.call_tool(
                "execute_decision", {"decision_id": BELOW_RISK_FLOOR_DECISION_ID, "webhook_url": "http://x"}
            ),
        )
    assert exc_info.value.gate == "risk_floor"
    assert exc_info.value.denial is not None
    assert exc_info.value.denial.code == "execution_blocked_risk_floor"
    assert exc_info.value.denial.override_hint is not None


async def test_idempotency_gate_blocks_second_delivery_and_force_re_executes(stub_server) -> None:
    base_url, _engine = stub_server

    async def scenario(client: AlgentaMcpClient) -> None:
        first = await client.call_tool("execute_decision", {"decision_id": "decision-1", "webhook_url": "http://x"})
        assert first["execution_status"] == "delivered"

        with pytest.raises(AlgentaExecutionBlocked) as exc_info:
            await client.call_tool("execute_decision", {"decision_id": "decision-1", "webhook_url": "http://x"})
        assert exc_info.value.gate == "idempotency"

        forced = await client.call_tool(
            "execute_decision", {"decision_id": "decision-1", "webhook_url": "http://x", "force": True}
        )
        assert forced["execution_status"] == "delivered"

    await with_session_resilient(base_url=base_url, profile="execute", fn=scenario)


async def test_confidence_gate_cleared_by_explicit_operator_override(stub_server) -> None:
    base_url, _engine = stub_server
    receipt = await with_session_resilient(
        base_url=base_url,
        profile="execute",
        fn=lambda c: c.call_tool(
            "execute_decision",
            {"decision_id": LOW_CONFIDENCE_DECISION_ID, "webhook_url": "http://x", "override_safety": True},
        ),
    )
    assert receipt["safety_overridden"] is True


async def test_generic_tool_error_raises_tool_call_failed_not_blocked(stub_server) -> None:
    base_url, _engine = stub_server
    with pytest.raises(AlgentaToolCallFailed) as exc_info:
        await with_session_resilient(
            base_url=base_url, profile="observe", fn=lambda c: c.call_tool("query_data", {"dataset": FLAKY_DATASET})
        )
    assert "temporarily unavailable" in str(exc_info.value.payload)


async def test_client_requires_context_manager(stub_server) -> None:
    base_url, _engine = stub_server
    client = AlgentaMcpClient(base_url=base_url)
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.call_tool("simulate", {"scenario": "x"})
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.list_tool_names()


def test_base_url_defaults_to_self_hosted_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALGENTA_BASE_URL", raising=False)
    assert AlgentaMcpClient().base_url == DEFAULT_ALGENTA_BASE_URL
    assert AlgentaMcpClient().base_url.startswith("http://localhost:")


def test_base_url_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALGENTA_BASE_URL", "http://engine.internal:9000/mcp")
    assert AlgentaMcpClient().base_url == "http://engine.internal:9000/mcp"
    assert AlgentaMcpClient(base_url="http://explicit:1/mcp").base_url == "http://explicit:1/mcp"


def test_extract_call_tool_payload_prefers_structured_content() -> None:
    result = CallToolResult(content=[], structuredContent={"a": 1}, isError=False)
    assert extract_call_tool_payload(result) == {"a": 1}


def test_extract_call_tool_payload_parses_text_json() -> None:
    result = CallToolResult(content=[TextContent(type="text", text='{"a": 1}')], isError=False)
    assert extract_call_tool_payload(result) == {"a": 1}


def test_extract_call_tool_payload_returns_non_json_text_verbatim() -> None:
    # So parse_denial's embedded-JSON recovery still gets a chance at prose-wrapped bodies.
    result = CallToolResult(content=[TextContent(type="text", text="Error: {\"error\": {}}")], isError=True)
    assert extract_call_tool_payload(result) == "Error: {\"error\": {}}"


def test_extract_call_tool_payload_returns_none_without_content() -> None:
    assert extract_call_tool_payload(CallToolResult(content=[], isError=False)) is None
