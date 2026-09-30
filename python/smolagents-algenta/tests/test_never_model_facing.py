"""`force` / `override_safety` must never reach the model -- neither as a schema property
(`get_tools`) nor as a value that actually makes it into the wrapped tool call (`call_tool`),
even if something upstream of `AlgentaToolset` (a hallucinating model, a hand-rolled caller)
tries to pass one anyway.
"""

from __future__ import annotations

import pytest
from smolagents import Tool
from smolagents_algenta import AlgentaToolset

received_calls: list[dict] = []


class _ExecuteDecision(Tool):
    name = "execute_decision"
    description = "Execute a decision."
    inputs = {
        "decision_id": {"type": "string", "description": "id"},
        "webhook_url": {"type": "string", "description": "url"},
        "force": {"type": "boolean", "description": "force"},
        "override_safety": {"type": "boolean", "description": "override"},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, decision_id: str, webhook_url: str, force: bool = False, override_safety: bool = False) -> dict:
        received_calls.append(
            {"decision_id": decision_id, "webhook_url": webhook_url, "force": force, "override_safety": override_safety}
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


def test_force_and_override_safety_are_absent_from_the_advertised_schema() -> None:
    with AlgentaToolset(wrapped=[_ExecuteDecision()], profile="execute") as toolset:
        tool = toolset.tools[0]
        assert "force" not in tool.inputs
        assert "override_safety" not in tool.inputs
        # And the fields that ARE supposed to be model-facing survive untouched.
        assert "decision_id" in tool.inputs
        assert "webhook_url" in tool.inputs


def test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call() -> None:
    with AlgentaToolset(wrapped=[_ExecuteDecision()], profile="execute") as toolset:
        tool = toolset.tools[0]

        # Simulates a caller/model that somehow still supplied `force`/`override_safety` despite
        # the schema not advertising them (e.g. copied from an earlier message).
        result = tool.forward(decision_id="dec-1", webhook_url="https://example.com/hook", force=True, override_safety=True)

        assert len(received_calls) == 1
        # The underlying tool never saw `force=True`/`override_safety=True` -- it saw its own
        # defaults, because AlgentaToolset.call_tool scrubbed both keys out of the arguments dict
        # before forwarding the call.
        assert received_calls[0] == {
            "decision_id": "dec-1",
            "webhook_url": "https://example.com/hook",
            "force": False,
            "override_safety": False,
        }
        assert result.decision_id == "dec-1"


def test_schema_without_never_model_facing_fields_is_returned_unchanged() -> None:
    # A tool whose schema never had `force`/`override_safety` to begin with shouldn't be
    # needlessly rebuilt -- `_strip_never_model_facing_inputs` returns the very same object it
    # was given, not just an equal-looking copy.
    from smolagents_algenta.toolset import _strip_never_model_facing_inputs

    inputs = {"scenario": {"type": "string", "description": "scenario"}}
    assert _strip_never_model_facing_inputs(inputs) is inputs
