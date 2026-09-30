"""Tool-profile filtering: an `"observe"`-profile toolset must not even *list* execute-tier
tools to the model, not just document them as unavailable.

Uses ADK `FunctionTool`s and the `tools=` escape hatch rather than the network stub server --
profile filtering is pure `get_tools()` logic and doesn't need a real MCP round trip to exercise.
"""

from __future__ import annotations

import pytest
from google.adk.tools import FunctionTool
from google_adk_algenta import AlgentaToolset

from .helpers import bare_tool_context


def get_contract() -> dict:
    return {"capabilities": []}


def query_data(dataset: str) -> dict:
    return {"dataset": dataset, "rows": []}


def simulate(scenario: str) -> dict:
    return {"scenario": scenario, "expected_value": 1.0}


def recommend(scenario: str) -> dict:
    return {"scenario": scenario, "recommended_action": "hold"}


def plan_decision(scenario: str) -> dict:
    return {"summary": "proposed plan", "scenario": scenario}


def log_decision(chosen_action: str) -> dict:
    return {"decision_id": "dec-1", "chosen_action": chosen_action}


def execute_decision(decision_id: str, webhook_url: str) -> dict:
    return {"decision_id": decision_id, "webhook_url": webhook_url, "execution_status": "delivered"}


def admin_only_diagnostic_tool() -> dict:
    """Not in the contract at all -- represents part of a real server's wider registry that
    only the `"full"` profile should ever see."""
    return {"ok": True}


ALL_TOOL_FUNCS = [
    get_contract,
    query_data,
    simulate,
    recommend,
    plan_decision,
    log_decision,
    execute_decision,
    admin_only_diagnostic_tool,
]


def make_toolset(profile: str) -> AlgentaToolset:
    return AlgentaToolset(tools=[FunctionTool(func) for func in ALL_TOOL_FUNCS], profile=profile)


@pytest.mark.anyio
async def test_observe_profile_exposes_exactly_the_contracts_observe_tools() -> None:
    toolset = make_toolset("observe")
    tools = await toolset.get_tools(bare_tool_context())
    assert set(tool.name for tool in tools) == {
        "get_contract",
        "query_data",
        "simulate",
        "recommend",
    }


@pytest.mark.anyio
async def test_observe_profile_never_lists_execute_decision() -> None:
    # The specific, explicit assertion the task calls out: not just "undocumented", genuinely
    # absent from what the model is told exists.
    toolset = make_toolset("observe")
    tools = await toolset.get_tools(bare_tool_context())
    assert "execute_decision" not in set(tool.name for tool in tools)
    assert "plan_decision" not in set(tool.name for tool in tools)
    assert "log_decision" not in set(tool.name for tool in tools)


@pytest.mark.anyio
async def test_govern_profile_adds_plan_and_log_but_not_execute() -> None:
    toolset = make_toolset("govern")
    tools = await toolset.get_tools(bare_tool_context())
    assert set(tool.name for tool in tools) == {
        "get_contract",
        "query_data",
        "simulate",
        "recommend",
        "plan_decision",
        "log_decision",
    }
    assert "execute_decision" not in set(tool.name for tool in tools)


@pytest.mark.anyio
async def test_execute_profile_adds_execute_decision() -> None:
    toolset = make_toolset("execute")
    tools = await toolset.get_tools(bare_tool_context())
    assert set(tool.name for tool in tools) == {
        "get_contract",
        "query_data",
        "simulate",
        "recommend",
        "plan_decision",
        "log_decision",
        "execute_decision",
    }
    assert "admin_only_diagnostic_tool" not in set(tool.name for tool in tools)


@pytest.mark.anyio
async def test_full_profile_exposes_everything_the_server_advertises() -> None:
    toolset = make_toolset("full")
    tools = await toolset.get_tools(bare_tool_context())
    assert set(tool.name for tool in tools) == set(name.__name__ for name in ALL_TOOL_FUNCS)
    assert "admin_only_diagnostic_tool" in set(tool.name for tool in tools)


def test_unknown_profile_rejected_at_construction_time() -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        AlgentaToolset(tools=[FunctionTool(func) for func in ALL_TOOL_FUNCS], profile="admin")  # type: ignore[arg-type]


def test_default_profile_is_observe() -> None:
    toolset = AlgentaToolset(tools=[FunctionTool(func) for func in ALL_TOOL_FUNCS])
    assert toolset.profile == "observe"
