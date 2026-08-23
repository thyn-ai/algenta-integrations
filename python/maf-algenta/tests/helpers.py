"""Shared, non-fixture test helpers: a fake in-memory MCP tool registry.

Used by `test_profile_filtering.py` and `test_never_model_facing.py`, both of which are pure
list-filtering / schema-scrubbing logic that doesn't need a real MCP round trip to exercise (see
`test_toolset_scenarios.py` for the real-wire equivalent, including the real approval gate and
the real governance mapping end to end).

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
_PLAN_HASH_SCHEMA = {"type": "object", "properties": {"plan_hash": {"type": "string"}}, "required": ["plan_hash"]}
_EXECUTE_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "plan_hash": {"type": "string"},
        "idempotency_key": {"type": "string", "default": "idem-1"},
        "force": {"type": "boolean", "default": False},
        "override_safety": {"type": "boolean", "default": False},
    },
    "required": ["plan_hash"],
}


def fake_mcp_tool(name: str, *, schema: dict[str, Any], func: Callable[..., Awaitable[Any]]) -> FunctionTool:
    return FunctionTool(name=name, description=f"fake {name}", input_model=schema, func=func)


@dataclass
class FakeMCPTool:
    """Stands in for an already-connected `agent_framework.MCPTool` in a test: the only thing
    `create_algenta_tools(mcp_tool=...)` reads off it is `.functions`."""

    functions: list[FunctionTool] = field(default_factory=list)


def _ok_receipt(**result: Any) -> dict[str, Any]:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": result}


def build_full_fake_registry(*, received_execute_calls: list[dict[str, Any]] | None = None) -> FakeMCPTool:
    """A fake registry covering every contract tool plus one tool the contract doesn't name at
    all (`admin_only_diagnostic_tool`) -- representing the wider real registry only `"full"`
    should ever expose.
    """

    async def get_contract() -> dict[str, Any]:
        return {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

    async def query_data(dataset: str) -> dict[str, Any]:
        return _ok_receipt(dataset=dataset)

    async def simulate(scenario: str) -> dict[str, Any]:
        return _ok_receipt(scenario=scenario)

    async def recommend(scenario: str) -> dict[str, Any]:
        return _ok_receipt(scenario=scenario)

    async def plan_decision(scenario: str) -> dict[str, Any]:
        return {"status": "ok", "code": "ok", "approval_state": "none", "plan_hash": "plan-1", "result": {}}

    async def log_decision(plan_hash: str) -> dict[str, Any]:
        return {"status": "ok", "code": "ok", "approval_state": "none", "plan_hash": plan_hash, "result": {}}

    async def execute_decision(
        plan_hash: str, idempotency_key: str = "idem-1", force: bool = False, override_safety: bool = False
    ) -> dict[str, Any]:
        if received_execute_calls is not None:
            received_execute_calls.append(
                {"plan_hash": plan_hash, "force": force, "override_safety": override_safety}
            )
        return {
            "status": "ok",
            "code": "ok",
            "approval_state": "approved",
            "plan_hash": plan_hash,
            "result": {"executed": True},
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
            fake_mcp_tool("log_decision", schema=_PLAN_HASH_SCHEMA, func=log_decision),
            fake_mcp_tool("execute_decision", schema=_EXECUTE_DECISION_SCHEMA, func=execute_decision),
            fake_mcp_tool("admin_only_diagnostic_tool", schema=_NO_ARGS_SCHEMA, func=admin_only_diagnostic_tool),
        ]
    )


__all__ = [
    "FakeMCPTool",
    "build_full_fake_registry",
    "fake_mcp_tool",
]
