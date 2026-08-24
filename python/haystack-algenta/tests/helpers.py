"""Shared, non-fixture test helpers: a fake in-memory `Tool` registry.

Used by `test_profile_filtering.py` and `test_never_model_facing.py`, both of which are pure
list-filtering / schema-scrubbing logic that doesn't need a real MCP round trip to exercise (see
`test_toolset_scenarios.py` for the real-wire equivalent, including the real 409 denial mapping and
the real receipt-mapping hook end to end).
"""

from __future__ import annotations

from typing import Any

from haystack.tools import Tool

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}}
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

    def query_data(scenario: str) -> dict[str, Any]:
        return {"dataset": scenario, "rows": []}

    def simulate(scenario: str) -> dict[str, Any]:
        return {"scenario": scenario, "expected_value": 1.0}

    def recommend(scenario: str) -> dict[str, Any]:
        return {"scenario": scenario, "recommended_action": "hold"}

    def plan_decision(scenario: str) -> dict[str, Any]:
        return {"plan_id": "plan-1", "scenario": scenario}

    def log_decision(chosen_action: str) -> dict[str, Any]:
        return {"decision_id": "decision-1", "chosen_action": chosen_action, "created_at": "2026-08-23T00:00:00Z"}

    def execute_decision(
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
            "executed_at": "2026-08-23T00:00:00Z",
            "policy_snapshot_id": "policy-1",
            "schema_snapshot_id": "schema-1",
            "manifest_version": "1",
            "payload_summary": None,
            "safety_overridden": override_safety,
        }

    def admin_only_diagnostic_tool() -> dict[str, Any]:
        return {"ok": True}

    return [
        Tool(name="get_contract", description="fake get_contract", parameters=_NO_ARGS_SCHEMA, function=get_contract),
        Tool(name="query_data", description="fake query_data", parameters=_SCENARIO_SCHEMA, function=query_data),
        Tool(name="simulate", description="fake simulate", parameters=_SCENARIO_SCHEMA, function=simulate),
        Tool(name="recommend", description="fake recommend", parameters=_SCENARIO_SCHEMA, function=recommend),
        Tool(name="plan_decision", description="fake plan_decision", parameters=_SCENARIO_SCHEMA, function=plan_decision),
        Tool(name="log_decision", description="fake log_decision", parameters=_LOG_DECISION_SCHEMA, function=log_decision),
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
