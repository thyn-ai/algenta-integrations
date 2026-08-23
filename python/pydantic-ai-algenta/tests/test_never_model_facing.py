"""`force` / `override_safety` must never reach the model -- neither as a schema property
(`get_tools`) nor as a value that actually makes it into the wrapped tool call (`call_tool`),
even if something upstream of `AlgentaToolset` (a hallucinating model, a hand-rolled caller)
tries to pass one anyway.
"""

from __future__ import annotations

import pytest
from pydantic_ai.toolsets.function import FunctionToolset

from pydantic_ai_algenta import AlgentaToolset

from .helpers import bare_run_context

received_calls: list[dict] = []


def execute_decision(plan_hash: str, force: bool = False, override_safety: bool = False) -> dict:
    """A fake execute_decision whose schema (like the real one) carries operator-only fields."""
    received_calls.append({"plan_hash": plan_hash, "force": force, "override_safety": override_safety})
    return {"status": "ok", "code": "ok", "approval_state": "approved", "plan_hash": plan_hash, "result": {}}


@pytest.fixture(autouse=True)
def _clear_received_calls() -> None:
    received_calls.clear()


@pytest.mark.anyio
async def test_force_and_override_safety_are_absent_from_the_advertised_schema() -> None:
    toolset = AlgentaToolset(wrapped=FunctionToolset([execute_decision]), profile="execute")
    tools = await toolset.get_tools(bare_run_context())
    schema = tools["execute_decision"].tool_def.parameters_json_schema
    assert "force" not in schema.get("properties", {})
    assert "override_safety" not in schema.get("properties", {})
    assert "force" not in schema.get("required", [])
    # And the field that IS supposed to be model-facing survives untouched.
    assert "plan_hash" in schema.get("properties", {})


@pytest.mark.anyio
async def test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call() -> None:
    toolset = AlgentaToolset(wrapped=FunctionToolset([execute_decision]), profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)
    tool = tools["execute_decision"]

    # Simulates a caller/model that somehow still supplied `force`/`override_safety` despite
    # the schema not advertising them (e.g. copied from an earlier message).
    result = await toolset.call_tool(
        "execute_decision",
        {"plan_hash": "plan-1", "force": True, "override_safety": True},
        ctx,
        tool,
    )

    assert len(received_calls) == 1
    # The underlying tool never saw `force=True`/`override_safety=True` -- it saw its own
    # defaults, because AlgentaToolset.call_tool scrubbed both keys out of the arguments dict
    # before forwarding the call.
    assert received_calls[0] == {"plan_hash": "plan-1", "force": False, "override_safety": False}
    assert result.plan_hash == "plan-1"


@pytest.mark.anyio
async def test_schema_without_never_model_facing_fields_is_returned_unchanged() -> None:
    # A tool whose schema never had `force`/`override_safety` to begin with shouldn't be
    # needlessly rebuilt -- `_strip_never_model_facing_tool` returns the very same object it
    # was given, not just an equal-looking copy.
    from pydantic_ai_algenta.toolset import _strip_never_model_facing_tool

    def recommend(scenario: str) -> dict:
        return {"ok": True}

    ctx = bare_run_context()
    tools = await FunctionToolset([recommend]).get_tools(ctx)
    original_tool = tools["recommend"]

    assert _strip_never_model_facing_tool(original_tool) is original_tool
