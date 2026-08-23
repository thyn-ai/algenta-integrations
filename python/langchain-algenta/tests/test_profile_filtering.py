"""Tool-profile filtering: an `"observe"`-profile call to `create_algenta_tools` must not even
*list* execute-tier tools, not just document them as unavailable.

Uses the in-memory `tools=` escape hatch rather than the network stub server -- profile
filtering is pure list-filtering logic and doesn't need a real MCP round trip to exercise (see
`tests/test_toolset_scenarios.py` for the real-wire equivalent).
"""

from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool

from langchain_algenta import create_algenta_tools

from .helpers import mcp_shaped_tool

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "required": []}
_SCENARIO_SCHEMA = {"type": "object", "properties": {"scenario": {"type": "string"}}, "required": ["scenario"]}
_PLAN_HASH_SCHEMA = {"type": "object", "properties": {"plan_hash": {"type": "string"}}, "required": ["plan_hash"]}


async def _get_contract() -> dict:
    return {"capabilities": []}


async def _query_data(dataset: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": {"dataset": dataset}}


async def _simulate(scenario: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": {"scenario": scenario}}


async def _recommend(scenario: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": {"scenario": scenario}}


async def _plan_decision(scenario: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "none", "plan_hash": "p1", "result": {}}


async def _log_decision(plan_hash: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": {}}


async def _execute_decision(plan_hash: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "approved", "result": {}}


async def _admin_only_diagnostic_tool() -> dict:
    """Not in the contract at all -- represents part of a real server's wider registry that
    only the `"full"` profile should ever see."""
    return {"ok": True}


def _all_fake_tools() -> list[BaseTool]:
    return [
        mcp_shaped_tool("get_contract", schema=_NO_ARGS_SCHEMA, coroutine=_get_contract),
        mcp_shaped_tool("query_data", schema=_SCENARIO_SCHEMA, coroutine=_query_data),
        mcp_shaped_tool("simulate", schema=_SCENARIO_SCHEMA, coroutine=_simulate),
        mcp_shaped_tool("recommend", schema=_SCENARIO_SCHEMA, coroutine=_recommend),
        mcp_shaped_tool("plan_decision", schema=_SCENARIO_SCHEMA, coroutine=_plan_decision),
        mcp_shaped_tool("log_decision", schema=_PLAN_HASH_SCHEMA, coroutine=_log_decision),
        mcp_shaped_tool("execute_decision", schema=_PLAN_HASH_SCHEMA, coroutine=_execute_decision),
        mcp_shaped_tool("admin_only_diagnostic_tool", schema=_NO_ARGS_SCHEMA, coroutine=_admin_only_diagnostic_tool),
    ]


async def test_observe_profile_exposes_exactly_the_contracts_observe_tools() -> None:
    tools = await create_algenta_tools(tools=_all_fake_tools(), profile="observe")
    assert {t.name for t in tools} == {"get_contract", "query_data", "simulate", "recommend"}


async def test_observe_profile_never_lists_execute_decision() -> None:
    # The specific, explicit assertion the task calls out: not just "undocumented", genuinely
    # absent from what the model is told exists.
    tools = await create_algenta_tools(tools=_all_fake_tools(), profile="observe")
    names = {t.name for t in tools}
    assert "execute_decision" not in names
    assert "plan_decision" not in names
    assert "log_decision" not in names


async def test_govern_profile_adds_plan_and_log_but_not_execute() -> None:
    tools = await create_algenta_tools(tools=_all_fake_tools(), profile="govern")
    names = {t.name for t in tools}
    assert names == {"get_contract", "query_data", "simulate", "recommend", "plan_decision", "log_decision"}
    assert "execute_decision" not in names


async def test_execute_profile_adds_execute_decision() -> None:
    tools = await create_algenta_tools(tools=_all_fake_tools(), profile="execute")
    names = {t.name for t in tools}
    assert names == {
        "get_contract",
        "query_data",
        "simulate",
        "recommend",
        "plan_decision",
        "log_decision",
        "execute_decision",
    }
    assert "admin_only_diagnostic_tool" not in names


async def test_full_profile_exposes_everything_the_server_advertises() -> None:
    tools = await create_algenta_tools(tools=_all_fake_tools(), profile="full")
    names = {t.name for t in tools}
    assert names == {tool.name for tool in _all_fake_tools()}
    assert "admin_only_diagnostic_tool" in names


async def test_unknown_profile_rejected_at_construction_time() -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        await create_algenta_tools(tools=_all_fake_tools(), profile="admin")  # type: ignore[arg-type]


async def test_default_profile_is_observe() -> None:
    tools = await create_algenta_tools(tools=_all_fake_tools())
    assert {t.name for t in tools} == {"get_contract", "query_data", "simulate", "recommend"}


async def test_tools_and_connection_arguments_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not more than one"):
        await create_algenta_tools(tools=_all_fake_tools(), base_url="http://localhost:9/mcp")
