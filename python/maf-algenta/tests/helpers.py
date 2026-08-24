"""Shared, non-fixture test helpers: a fake in-memory MCP tool registry.

Used by `test_profile_filtering.py` and `test_never_model_facing.py`, both of which are pure
list-filtering / schema-scrubbing logic that doesn't need a real MCP round trip to exercise (see
`test_toolset_scenarios.py` for the real-wire equivalent, including the real denial mapping end
to end).

`fake_mcp_tool(name, schema=..., func=...)` builds a plain `agent_framework.FunctionTool` with a
raw JSON-Schema `dict` as its `input_model` -- the exact shape a real MCP-derived function's
`.parameters()` returns (`input_model` itself is `None` on a real MCP-loaded `FunctionTool`,
confirmed directly against the installed `agent_framework` 1.15.0; `.parameters()` still returns
the dict schema regardless -- constructing one directly with `input_model=<dict>` produces the
same observable shape, verified while building this package). `_FakeMCPTool` then stands in for
`agent_framework.MCPTool` well enough to satisfy `create_algenta_tools`'s `mcp_tool=` escape
hatch (it only ever reads `.functions` off that value).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agent_framework import FunctionTool

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}, "required": []}
_SCENARIO_SCHEMA = {"type": "object", "properties": {"scenario": {"type": "string"}}, "required": ["scenario"]}
_LOG_DECISION_SCHEMA = {
    "type": "object",
    "properties": {"chosen_action": {"type": "string"}},
    "required": ["chosen_action"],
}
_EXECUTE_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_id": {"type": "string"},
        "webhook_url": {"type": "string"},
        "force": {"type": "boolean", "default": False},
        "override_safety": {"type": "boolean", "default": False},
    },
    "required": ["decision_id", "webhook_url"],
}


def fake_mcp_tool(name: str, *, schema: dict[str, Any], func: Callable[..., Awaitable[Any]]) -> FunctionTool:
    return FunctionTool(name=name, description=f"fake {name}", input_model=schema, func=func)


@dataclass
class FakeMCPTool:
    """Stands in for an already-connected `agent_framework.MCPTool` in a test: the only thing
    `create_algenta_tools(mcp_tool=...)` reads off it is `.functions`."""

    functions: list[FunctionTool] = field(default_factory=list)


def build_full_fake_registry(*, received_execute_calls: list[dict[str, Any]] | None = None) -> FakeMCPTool:
    """A fake registry covering every contract tool plus one tool the contract doesn't name at
    all (`admin_only_diagnostic_tool`) -- representing the wider real registry only `"full"`
    should ever expose.
    """

    async def get_contract() -> dict[str, Any]:
        return {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

    async def query_data(dataset: str) -> dict[str, Any]:
        return {"dataset": dataset}

    async def simulate(scenario: str) -> dict[str, Any]:
        return {"scenario": scenario}

    async def recommend(scenario: str) -> dict[str, Any]:
        return {"scenario": scenario}

    async def plan_decision(scenario: str) -> dict[str, Any]:
        return {"scenario": scenario}

    async def log_decision(chosen_action: str) -> dict[str, Any]:
        return {
            "decision_id": "decision-1",
            "chosen_action": chosen_action,
            "expected_value": None,
            "confidence": None,
            "created_at": "2026-08-23T00:00:00+00:00",
            "note": "logged",
        }

    async def execute_decision(
        decision_id: str, webhook_url: str, force: bool = False, override_safety: bool = False
    ) -> dict[str, Any]:
        if received_execute_calls is not None:
            received_execute_calls.append(
                {"decision_id": decision_id, "force": force, "override_safety": override_safety}
            )
        return {
            "decision_id": decision_id,
            "webhook_url": webhook_url,
            "execution_status": "delivered",
            "response_code": 200,
            "executed_at": "2026-08-23T00:00:00+00:00",
            "policy_snapshot_id": "policy-snap-1",
            "schema_snapshot_id": "schema-snap-1",
            "manifest_version": "1",
            "payload_summary": None,
            "safety_overridden": override_safety,
        }

    async def admin_only_diagnostic_tool() -> dict[str, Any]:
        return {"ok": True}

    return FakeMCPTool(
        functions=[
            fake_mcp_tool("get_contract", schema=_NO_ARGS_SCHEMA, func=get_contract),
            fake_mcp_tool("query_data", schema=_SCENARIO_SCHEMA, func=query_data),
            fake_mcp_tool("simulate", schema=_SCENARIO_SCHEMA, func=simulate),
            fake_mcp_tool("recommend", schema=_SCENARIO_SCHEMA, func=recommend),
            fake_mcp_tool("plan_decision", schema=_SCENARIO_SCHEMA, func=plan_decision),
            fake_mcp_tool("log_decision", schema=_LOG_DECISION_SCHEMA, func=log_decision),
            fake_mcp_tool("execute_decision", schema=_EXECUTE_DECISION_SCHEMA, func=execute_decision),
            fake_mcp_tool("admin_only_diagnostic_tool", schema=_NO_ARGS_SCHEMA, func=admin_only_diagnostic_tool),
        ]
    )


__all__ = [
    "FakeMCPTool",
    "build_full_fake_registry",
    "fake_mcp_tool",
]
