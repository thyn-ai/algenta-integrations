"""`force`/`override_safety` must never reach the model -- neither as a schema property (the tool
list `create_algenta_tools` yields) nor as a value that actually makes it into the wrapped call,
even if something upstream (a hallucinating model, a hand-rolled caller) tries to pass one anyway.

Uses the `tools=` fake-registry escape hatch, with a fake `execute_decision` whose schema (like
the real one) carries the two operator-only fields. `tests/test_toolset_scenarios.py` additionally
proves the same scrub over the real wire against the stub server.
"""

from __future__ import annotations

from haystack_algenta import create_algenta_tools
from haystack_algenta.toolset import _strip_never_model_facing_schema

from .helpers import build_full_fake_registry


def test_force_and_override_safety_are_absent_from_the_advertised_schema() -> None:
    toolset = create_algenta_tools(tools=build_full_fake_registry(), profile="execute")
    execute_decision = next(t for t in toolset if t.name == "execute_decision")
    assert "force" not in execute_decision.parameters.get("properties", {})
    assert "override_safety" not in execute_decision.parameters.get("properties", {})
    assert "force" not in execute_decision.parameters.get("required", [])
    # And the field that IS supposed to be model-facing survives untouched.
    assert "plan_hash" in execute_decision.parameters.get("properties", {})


def test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call() -> None:
    received_calls: list[dict] = []
    registry = build_full_fake_registry(received_execute_calls=received_calls)
    toolset = create_algenta_tools(tools=registry, profile="execute")
    execute_decision = next(t for t in toolset if t.name == "execute_decision")

    # Simulates a caller/model that somehow still supplied `force`/`override_safety` despite the
    # schema not advertising them (e.g. copied from an earlier message).
    raw_result = execute_decision.invoke(plan_hash="plan-1", force=True, override_safety=True)

    assert len(received_calls) == 1
    # The underlying tool never saw `force=True`/`override_safety=True` -- it saw its own
    # defaults, because the call-time scrub removed both keys from the arguments dict before
    # forwarding the call.
    assert received_calls[0] == {"plan_hash": "plan-1", "force": False, "override_safety": False}
    assert raw_result == {
        "status": "ok",
        "code": "ok",
        "approval_state": "approved",
        "plan_hash": "plan-1",
        "result": {"executed": True},
    }


def test_schema_without_never_model_facing_fields_is_returned_unchanged() -> None:
    # A schema that never had `force`/`override_safety` to begin with shouldn't be needlessly
    # rebuilt -- `_strip_never_model_facing_schema` returns the very same object it was given, not
    # just an equal-looking copy.
    schema = {"type": "object", "properties": {"scenario": {"type": "string"}}, "required": ["scenario"]}
    assert _strip_never_model_facing_schema(schema) is schema
