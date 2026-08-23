"""Shared, non-fixture test helpers: a fake in-memory `Tool` registry.

Used by `test_profile_filtering.py` and `test_never_model_facing.py`, both of which are pure
list-filtering / schema-scrubbing logic that doesn't need a real MCP round trip to exercise (see
`test_toolset_scenarios.py` for the real-wire equivalent, including the real approval gate and the
real receipt-mapping hook end to end).
"""

from __future__ import annotations

from typing import Any

from haystack.tools import Tool

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}}
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


def _ok_receipt(**result: Any) -> dict[str, Any]:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": result}


def build_full_fake_registry(*, received_execute_calls: list[dict[str, Any]] | None = None) -> list[Tool]:
    """A fake registry covering every contract tool plus one tool the contract doesn't name at all
    (`admin_only_diagnostic_tool`) -- representing the wider real registry only `"full"` should
    ever expose.

    Every function here returns a plain `dict` directly (not wrapped in an MCP `CallToolResult`
    envelope) -- exactly like `maf_algenta`'s/`langchain_algenta`'s own fake registries, and a
    faithful stand-in for what `haystack_algenta.receipts.unwrap_mcp_tool_result` falls back to
    for a plain dict (see that function's docstring). The real double-JSON-string envelope is only
    exercised end to end in `test_toolset_scenarios.py`, against the real stub server.
    """

    def get_contract() -> dict[str, Any]:
        return {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

    def query_data(dataset: str) -> dict[str, Any]:
        return _ok_receipt(dataset=dataset)

    def simulate(scenario: str) -> dict[str, Any]:
        return _ok_receipt(scenario=scenario)

    def recommend(scenario: str) -> dict[str, Any]:
        return _ok_receipt(scenario=scenario)

    def plan_decision(scenario: str) -> dict[str, Any]:
        return {"status": "ok", "code": "ok", "approval_state": "none", "plan_hash": "plan-1", "result": {}}

    def log_decision(plan_hash: str) -> dict[str, Any]:
        return {"status": "ok", "code": "ok", "approval_state": "none", "plan_hash": plan_hash, "result": {}}

    def execute_decision(
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

    def admin_only_diagnostic_tool() -> dict[str, Any]:
        return {"ok": True}

    return [
        Tool(name="get_contract", description="fake get_contract", parameters=_NO_ARGS_SCHEMA, function=get_contract),
        Tool(name="query_data", description="fake query_data", parameters=_SCENARIO_SCHEMA, function=query_data),
        Tool(name="simulate", description="fake simulate", parameters=_SCENARIO_SCHEMA, function=simulate),
        Tool(name="recommend", description="fake recommend", parameters=_SCENARIO_SCHEMA, function=recommend),
        Tool(name="plan_decision", description="fake plan_decision", parameters=_SCENARIO_SCHEMA, function=plan_decision),
        Tool(name="log_decision", description="fake log_decision", parameters=_PLAN_HASH_SCHEMA, function=log_decision),
        Tool(
            name="execute_decision",
            description="fake execute_decision",
            parameters=_EXECUTE_DECISION_SCHEMA,
            function=execute_decision,
        ),
        Tool(
            name="admin_only_diagnostic_tool",
            description="fake admin_only_diagnostic_tool",
            parameters=_NO_ARGS_SCHEMA,
            function=admin_only_diagnostic_tool,
        ),
    ]


__all__ = ["build_full_fake_registry"]
