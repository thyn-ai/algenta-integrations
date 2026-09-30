"""`force` / `override_safety` must never reach the model -- neither as a schema property
(`get_tools`) nor as a value that actually makes it into the wrapped tool call (`run_async`),
even if something upstream of `AlgentaToolset` (a hallucinating model, a hand-rolled caller)
tries to pass one anyway.
"""

from __future__ import annotations

import pytest
from google.adk.tools import FunctionTool
from google_adk_algenta import AlgentaToolset

from .helpers import bare_tool_context

received_calls: list[dict] = []


def execute_decision(
    decision_id: str, webhook_url: str, force: bool = False, override_safety: bool = False
) -> dict:
    """A fake execute_decision whose schema (like the real one) carries operator-only fields."""
    received_calls.append(
        {
            "decision_id": decision_id,
            "webhook_url": webhook_url,
            "force": force,
            "override_safety": override_safety,
        }
    )
    return {
        "decision_id": decision_id,
        "webhook_url": webhook_url,
        "execution_status": "delivered",
        "safety_overridden": force or override_safety,
    }


@pytest.fixture(autouse=True)
def _clear_received_calls() -> None:
    received_calls.clear()


@pytest.mark.anyio
async def test_force_and_override_safety_are_absent_from_the_advertised_schema() -> None:
    toolset = AlgentaToolset(tools=[FunctionTool(execute_decision)], profile="execute")
    tools = await toolset.get_tools(bare_tool_context())
    schema = tools[0]._get_declaration().parameters_json_schema
    assert "force" not in schema.get("properties", {})
    assert "override_safety" not in schema.get("properties", {})
    assert "force" not in schema.get("required", [])
    # And the fields that ARE supposed to be model-facing survive untouched.
    assert "decision_id" in schema.get("properties", {})
    assert "webhook_url" in schema.get("properties", {})


@pytest.mark.anyio
async def test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call() -> None:
    toolset = AlgentaToolset(tools=[FunctionTool(execute_decision)], profile="execute")
    ctx = bare_tool_context()
    tools = await toolset.get_tools(ctx)
    tool = tools[0]

    # Simulates a caller/model that somehow still supplied `force`/`override_safety` despite
    # the schema not advertising them (e.g. copied from a prior message).
    result = await tool.run_async(
        args={
            "decision_id": "dec-1",
            "webhook_url": "https://example.com/hook",
            "force": True,
            "override_safety": True,
        },
        tool_context=ctx,
    )

    assert len(received_calls) == 1
    # The underlying tool never saw `force=True`/`override_safety=True` -- it saw its own
    # defaults, because AlgentaToolset's wrapper scrubbed both keys out of the arguments dict
    # before forwarding the call.
    assert received_calls[0] == {
        "decision_id": "dec-1",
        "webhook_url": "https://example.com/hook",
        "force": False,
        "override_safety": False,
    }
    assert result.decision_id == "dec-1"


@pytest.mark.anyio
async def test_schema_without_never_model_facing_fields_is_returned_unchanged() -> None:
    # A tool whose schema never had `force`/`override_safety` to begin with shouldn't be
    # needlessly rebuilt -- `_strip_function_declaration` returns the very same declaration it
    # was given, not just an equal-looking copy.
    from google_adk_algenta.toolset import _strip_function_declaration

    def recommend(scenario: str) -> dict:
        return {"ok": True}

    decl = FunctionTool(recommend)._get_declaration()
    assert _strip_function_declaration(decl) is decl
