"""`force`/`override_safety` must never reach the model -- neither as a schema property nor as a
value that actually makes it into the wrapped `call_tool(...)` call, even if something upstream
(a hallucinating model, a hand-rolled caller) tries to pass one anyway.

Exercised over the real wire against `tests/stub_server.py`'s real `execute_decision` tool, whose
schema (like the real one) genuinely carries `force`/`override_safety` -- this is
`create_algenta_tools` under test, not the server.
"""

from __future__ import annotations

import pytest
from llamaindex_algenta import ExecutionReceipt, create_algenta_tools

from .stub_server import FORCE_PROBE_DECISION_ID


@pytest.mark.asyncio
async def test_force_and_override_safety_are_absent_from_the_advertised_schema(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")
    schema = tool.metadata.get_parameters_dict()
    assert "force" not in schema.get("properties", {})
    assert "override_safety" not in schema.get("properties", {})
    assert "force" not in schema.get("required", [])
    # And the fields that ARE supposed to be model-facing survive untouched.
    assert "decision_id" in schema.get("properties", {})
    assert "webhook_url" in schema.get("properties", {})


@pytest.mark.asyncio
async def test_a_smuggled_force_argument_never_reaches_the_wrapped_tool_call(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    tool = next(t for t in tools if t.metadata.name == "execute_decision")

    # Simulates a caller/model that somehow still supplied `force`/`override_safety` despite the
    # schema not advertising them (e.g. copied from an earlier message).
    output = await tool.acall(
        decision_id=FORCE_PROBE_DECISION_ID,
        webhook_url="https://example.com/hook",
        force=True,
        override_safety=True,
    )

    assert isinstance(output.raw_output, ExecutionReceipt)
    # The underlying server never saw `force=True` -- it saw its own default, because the
    # wrapper's argument-level scrub stripped both keys before the real MCP call was ever made.
    assert output.raw_output.payload_summary == {"force_received": False}


@pytest.mark.asyncio
async def test_a_schema_without_never_model_facing_fields_is_left_untouched(stub_server: str) -> None:
    # `recommend` never had `force`/`override_safety` on its schema to begin with.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    tool = next(t for t in tools if t.metadata.name == "recommend")
    schema = tool.metadata.get_parameters_dict()
    assert "force" not in schema.get("properties", {})
    assert "scenario" in schema.get("properties", {})
