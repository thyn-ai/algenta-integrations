"""Tool-profile filtering: an `"observe"`-profile call to `create_algenta_tools` must not even
*list* execute-tier tools, not just document them as unavailable.

Uses the `mcp_tool=` fake-registry escape hatch rather than the network stub server -- profile
filtering is pure list-filtering logic and doesn't need a real MCP round trip to exercise (see
`tests/test_toolset_scenarios.py` for the real-wire equivalent).
"""

from __future__ import annotations

import pytest
from maf_algenta import create_algenta_tools

from .helpers import build_full_fake_registry


async def test_observe_profile_exposes_exactly_the_contracts_observe_tools() -> None:
    async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="observe") as tools:
        assert {t.name for t in tools} == {"get_contract", "query_data", "simulate", "recommend"}


async def test_observe_profile_never_lists_execute_decision() -> None:
    # The specific, explicit assertion the task calls out: not just "undocumented", genuinely
    # absent from what the model is told exists.
    async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="observe") as tools:
        names = {t.name for t in tools}
        assert "execute_decision" not in names
        assert "plan_decision" not in names
        assert "log_decision" not in names


async def test_govern_profile_adds_plan_and_log_but_not_execute() -> None:
    async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="govern") as tools:
        names = {t.name for t in tools}
        assert names == {"get_contract", "query_data", "simulate", "recommend", "plan_decision", "log_decision"}
        assert "execute_decision" not in names


async def test_execute_profile_adds_execute_decision() -> None:
    async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="execute") as tools:
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
    registry = build_full_fake_registry()
    async with create_algenta_tools(mcp_tool=registry, profile="full") as tools:
        names = {t.name for t in tools}
        assert names == {fn.name for fn in registry.functions}
        assert "admin_only_diagnostic_tool" in names


async def test_unknown_profile_rejected_at_construction_time() -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="admin"):  # type: ignore[arg-type]
            pass


async def test_default_profile_is_observe() -> None:
    async with create_algenta_tools(mcp_tool=build_full_fake_registry()) as tools:
        assert {t.name for t in tools} == {"get_contract", "query_data", "simulate", "recommend"}


async def test_mcp_tool_and_connection_arguments_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        async with create_algenta_tools(mcp_tool=build_full_fake_registry(), base_url="http://localhost:9/mcp"):
            pass


async def test_execute_decision_gets_no_pre_call_approval_gate() -> None:
    # The real engine's `execute_decision` has no async, pre-call approval state to gate on --
    # a call either succeeds synchronously or is denied synchronously, in the same call (see
    # `maf_algenta.receipts`). So, unlike an earlier version of this package, `execute_decision`
    # does NOT get MAF's `approval_mode="always_require"` pre-call gate: there is nothing for a
    # human to approve before the call, only a real, synchronous outcome to observe after it.
    async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="execute") as tools:
        execute_decision = next(t for t in tools if t.name == "execute_decision")
        assert execute_decision.approval_mode != "always_require"


async def test_no_tool_gets_a_pre_call_approval_gate() -> None:
    async with create_algenta_tools(mcp_tool=build_full_fake_registry(), profile="full") as tools:
        for tool in tools:
            assert tool.approval_mode != "always_require"
