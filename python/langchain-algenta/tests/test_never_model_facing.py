"""`force` / `override_safety` must never reach the model -- neither as a schema property (the
tool list `create_algenta_tools` returns) nor as a value that actually makes it into the wrapped
tool call, even if something upstream (a hallucinating model, a hand-rolled caller) tries to pass
one anyway.

Uses `create_algenta_tools(tools=...)` -- the in-memory escape hatch -- with a fake tool built
with a raw JSON-Schema `dict` `args_schema` (see `tests/helpers.mcp_shaped_tool`), the exact shape
every real MCP tool has. `tests/test_toolset_scenarios.py` additionally proves the same scrub over
the real wire against the stub server.
"""

from __future__ import annotations

from typing import Any

from langchain_algenta import create_algenta_tools
from langchain_algenta.toolset import _strip_never_model_facing_tool

from .helpers import mcp_shaped_tool

EXECUTE_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_hash": {"type": "string", "title": "Plan Hash"},
        "force": {"type": "boolean", "title": "Force", "default": False},
        "override_safety": {"type": "boolean", "title": "Override Safety", "default": False},
    },
    "required": ["plan_hash"],
    "title": "execute_decisionArguments",
}

received_calls: list[dict[str, Any]] = []


async def _execute_decision(plan_hash: str, force: bool = False, override_safety: bool = False) -> dict[str, Any]:
    """A fake execute_decision whose schema (like the real one) carries operator-only fields."""
    received_calls.append({"plan_hash": plan_hash, "force": force, "override_safety": override_safety})
    return {"status": "ok", "code": "ok", "approval_state": "approved", "plan_hash": plan_hash, "result": {}}


def _make_execute_decision_tool():
    received_calls.clear()
    return mcp_shaped_tool("execute_decision", schema=EXECUTE_DECISION_SCHEMA, coroutine=_execute_decision)


async def test_force_and_override_safety_are_absent_from_the_advertised_schema() -> None:
    tools = await create_algenta_tools(tools=[_make_execute_decision_tool()], profile="execute")
    schema = tools[0].args_schema
    assert "force" not in schema.get("properties", {})
    assert "override_safety" not in schema.get("properties", {})
    assert "force" not in schema.get("required", [])
    # And the field that IS supposed to be model-facing survives untouched.
    assert "plan_hash" in schema.get("properties", {})


async def test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call() -> None:
    tools = await create_algenta_tools(tools=[_make_execute_decision_tool()], profile="execute")
    tool = tools[0]

    # Simulates a caller/model that somehow still supplied `force`/`override_safety` despite
    # the schema not advertising them (e.g. copied from an earlier message).
    result = await tool.ainvoke({"plan_hash": "plan-1", "force": True, "override_safety": True})

    assert len(received_calls) == 1
    # The underlying tool never saw `force=True`/`override_safety=True` -- it saw its own
    # defaults, because the never-model-facing scrub removed both keys from the arguments dict
    # before forwarding the call.
    assert received_calls[0] == {"plan_hash": "plan-1", "force": False, "override_safety": False}
    assert result == {"status": "ok", "code": "ok", "approval_state": "approved", "plan_hash": "plan-1", "result": {}}


def test_schema_without_never_model_facing_fields_is_returned_unchanged() -> None:
    # A tool whose schema never had `force`/`override_safety` to begin with shouldn't be
    # needlessly rebuilt -- `_strip_never_model_facing_tool` returns the very same object it
    # was given, not just an equal-looking copy.
    async def recommend(scenario: str) -> dict:
        return {"ok": True}

    original_tool = mcp_shaped_tool(
        "recommend",
        schema={"type": "object", "properties": {"scenario": {"type": "string"}}, "required": ["scenario"]},
        coroutine=recommend,
    )

    assert _strip_never_model_facing_tool(original_tool) is original_tool
