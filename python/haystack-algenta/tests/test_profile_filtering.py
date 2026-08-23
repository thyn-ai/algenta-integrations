"""Tool-profile filtering: an `"observe"`-profile call to `create_algenta_tools` must not even
*list* execute-tier tools, not just document them as unavailable.

Uses the `tools=` fake-registry escape hatch rather than the network stub server -- profile
filtering is pure list-filtering logic and doesn't need a real MCP round trip to exercise (see
`tests/test_toolset_scenarios.py` for the real-wire equivalent, where the primary `base_url=` path
filters natively via `MCPToolset(tool_names=...)`).
"""

from __future__ import annotations

import pytest

from haystack_algenta import create_algenta_tools

from .helpers import build_full_fake_registry


def test_observe_profile_exposes_exactly_the_contracts_observe_tools() -> None:
    toolset = create_algenta_tools(tools=build_full_fake_registry(), profile="observe")
    assert {t.name for t in toolset} == {"get_contract", "query_data", "simulate", "recommend"}


def test_observe_profile_never_lists_execute_decision() -> None:
    # The specific, explicit assertion the task calls out: not just "undocumented", genuinely
    # absent from what the model is told exists.
    toolset = create_algenta_tools(tools=build_full_fake_registry(), profile="observe")
    names = {t.name for t in toolset}
    assert "execute_decision" not in names
    assert "plan_decision" not in names
    assert "log_decision" not in names


def test_govern_profile_adds_plan_and_log_but_not_execute() -> None:
    toolset = create_algenta_tools(tools=build_full_fake_registry(), profile="govern")
    names = {t.name for t in toolset}
    assert names == {"get_contract", "query_data", "simulate", "recommend", "plan_decision", "log_decision"}
    assert "execute_decision" not in names


def test_execute_profile_adds_execute_decision() -> None:
    toolset = create_algenta_tools(tools=build_full_fake_registry(), profile="execute")
    names = {t.name for t in toolset}
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


def test_full_profile_exposes_everything_the_server_advertises() -> None:
    registry = build_full_fake_registry()
    toolset = create_algenta_tools(tools=registry, profile="full")
    names = {t.name for t in toolset}
    assert names == {t.name for t in registry}
    assert "admin_only_diagnostic_tool" in names


def test_unknown_profile_rejected_at_construction_time() -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        create_algenta_tools(tools=build_full_fake_registry(), profile="admin")  # type: ignore[arg-type]


def test_default_profile_is_observe() -> None:
    toolset = create_algenta_tools(tools=build_full_fake_registry())
    assert {t.name for t in toolset} == {"get_contract", "query_data", "simulate", "recommend"}


def test_tools_and_connection_arguments_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        create_algenta_tools(tools=build_full_fake_registry(), base_url="http://localhost:9/mcp")


def test_returned_toolset_close_is_a_no_op_for_the_tools_escape_hatch() -> None:
    toolset = create_algenta_tools(tools=build_full_fake_registry(), profile="observe")
    toolset.close()  # must not raise -- this package never owned a connection to tear down
