"""End-to-end scenarios against the real stub Algenta MCP server, driven through a real
`haystack.components.agents.Agent` run loop (via `ScriptedChatGenerator`, not a mock of anything
in `haystack_algenta` or `haystack`/`mcp-haystack` themselves).

Every scenario below exercises the real `haystack_algenta.create_algenta_tools` -> real
`haystack_integrations.tools.mcp.MCPToolset` -> real `mcp` client -> real wire ->
`tests/stub_server.py` round trip, and the real Haystack `Agent` step loop (real tool invocation,
the `after_tool` `GovernedReceiptHook`, exception propagation) -- nothing about the denial, the
failure, or the never-model-facing scrub is asserted by inspecting `haystack_algenta`'s internals
directly; each is proven by actually running an agent and observing what came back or what was
raised.
"""

from __future__ import annotations

from typing import Any

import pytest
from haystack.components.agents import Agent
from haystack.hooks.human_in_the_loop import (
    AlwaysAskPolicy,
    BlockingConfirmationStrategy,
    ConfirmationHook,
    ConfirmationUIResult,
)
from haystack.tools.errors import ToolInvocationError
from haystack_algenta import AlgentaToolDenied, build_algenta_governance_hooks, create_algenta_tools
from haystack_algenta.receipts import (
    ExecutionBlocked,
    ExecutionReceipt,
    extract_execution_outcome_from_tool_result,
    unwrap_mcp_tool_result,
)
from haystack_integrations.tools.mcp import MCPToolset, StreamableHttpServerInfo

from .fake_chat_generator import ScriptedChatGenerator, text_reply, tool_call_reply
from .stub_server import CONFIDENCE_BLOCKED_DECISION_ID, RISK_FLOOR_BLOCKED_DECISION_ID

WEBHOOK_URL = "https://example.test/hook"


class _ScriptedUI:
    """A `ConfirmationUI` that returns a scripted decision without asking a real human."""

    def __init__(self, action: str) -> None:
        self.action = action
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_user_confirmation(self, tool_name: str, tool_description: str, tool_params: dict[str, Any]) -> ConfirmationUIResult:
        self.calls.append((tool_name, dict(tool_params)))
        return ConfirmationUIResult(action=self.action)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tests.test_toolset_scenarios._ScriptedUI", "init_parameters": {"action": self.action}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _ScriptedUI:
        return cls(**data.get("init_parameters", {}))


def test_query_data_success_via_real_agent_run(stub_server: str) -> None:
    toolset = create_algenta_tools(base_url=stub_server, profile="observe")
    generator = ScriptedChatGenerator(
        [tool_call_reply("query_data", {"dataset": "widgets"}), text_reply("done")]
    )
    agent = Agent(chat_generator=generator, tools=toolset)
    agent.warm_up()
    result = agent.run(messages=[])
    assert result["messages"][-1].text == "done"
    tool_result_messages = [m for m in result["messages"] if m.tool_call_result is not None]
    assert tool_result_messages and "widgets" in tool_result_messages[0].tool_call_result.result
    toolset.close()


def test_get_contract_non_envelope_result_passes_through(stub_server: str) -> None:
    toolset = create_algenta_tools(base_url=stub_server, profile="observe")
    get_contract = next(t for t in toolset if t.name == "get_contract")
    raw_result = get_contract.invoke()
    assert "capabilities" in raw_result  # the raw MCP envelope text, not parsed -- see receipts.py
    toolset.close()


def test_query_data_success_via_direct_invoke(stub_server: str) -> None:
    # query_data has its own result shape -- it never validates as an execute_decision outcome
    # (no webhook_url/execution_status), so extract_execution_outcome_from_tool_result must pass
    # it through as None, exactly like get_contract's discovery payload.
    toolset = create_algenta_tools(base_url=stub_server, profile="observe")
    query_data = next(t for t in toolset if t.name == "query_data")
    raw_result = query_data.invoke(dataset="widgets")
    assert extract_execution_outcome_from_tool_result(raw_result) is None
    assert unwrap_mcp_tool_result(raw_result) == {"dataset": "widgets", "rows": [{"value": 1}, {"value": 2}]}
    toolset.close()


def test_a_confirmation_hook_can_still_be_wired_directly_for_a_pre_call_gate(stub_server: str) -> None:
    """`haystack_algenta` no longer builds a pre-call gate itself (there is nothing left to confirm
    pre-call for `execute_decision` -- see `haystack_algenta.hooks`'s docstring), but Haystack's
    own `ConfirmationHook` is a perfectly real, general-purpose primitive a caller can still wire
    directly if they want a human-in-the-loop gate for their own reasons. Proven live: with
    `AlwaysAskPolicy()` and a UI that rejects, the real MCP server is genuinely never contacted."""
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    ui = _ScriptedUI(action="reject")
    hook = ConfirmationHook(
        confirmation_strategies={
            "execute_decision": BlockingConfirmationStrategy(confirmation_policy=AlwaysAskPolicy(), confirmation_ui=ui)
        }
    )
    generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"decision_id": "decision-never-called", "webhook_url": WEBHOOK_URL})]
    )
    agent = Agent(chat_generator=generator, tools=toolset, hooks={"before_tool": [hook]})
    agent.warm_up()
    result = agent.run(messages=[])

    assert ui.calls, "the confirmation UI must actually have been asked"
    tool_result_messages = [m for m in result["messages"] if m.tool_call_result is not None]
    assert tool_result_messages
    assert tool_result_messages[0].tool_call_result.error is True
    assert "rejected" in tool_result_messages[0].tool_call_result.result
    toolset.close()


def test_execute_decision_success_round_trip_via_real_agent_run(stub_server: str) -> None:
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    hooks = build_algenta_governance_hooks()
    generator = ScriptedChatGenerator(
        [
            tool_call_reply("execute_decision", {"decision_id": "decision-full-success", "webhook_url": WEBHOOK_URL}),
            text_reply("done"),
        ]
    )
    agent = Agent(chat_generator=generator, tools=toolset, hooks=hooks)
    agent.warm_up()

    result = agent.run(messages=[])
    assert result["messages"][-1].text == "done"
    tool_result_messages = [m for m in result["messages"] if m.tool_call_result is not None]
    assert tool_result_messages, "expected the real tool call to have actually happened"
    assert tool_result_messages[0].tool_call_result.error is False
    outcome = extract_execution_outcome_from_tool_result(tool_result_messages[0].tool_call_result.result)
    assert isinstance(outcome, ExecutionReceipt)
    assert outcome.is_delivered()
    toolset.close()


@pytest.mark.parametrize(
    ("decision_id", "expected_gate"),
    [(CONFIDENCE_BLOCKED_DECISION_ID, "confidence"), (RISK_FLOOR_BLOCKED_DECISION_ID, "risk_floor")],
)
def test_execute_decision_named_gate_denial_raises_tool_denied(stub_server: str, decision_id: str, expected_gate: str) -> None:
    """The real, synchronous 409 -- no pause, no pending state, just a same-call denial naming one
    of the three real gates -- surfaces as `AlgentaToolDenied` out of `agent.run()`, unmodified."""
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    hooks = build_algenta_governance_hooks()
    generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"decision_id": decision_id, "webhook_url": WEBHOOK_URL})]
    )
    agent = Agent(chat_generator=generator, tools=toolset, hooks=hooks)
    agent.warm_up()

    with pytest.raises(AlgentaToolDenied) as exc_info:
        agent.run(messages=[])

    assert exc_info.value.gate == expected_gate
    assert exc_info.value.blocked is not None
    assert exc_info.value.blocked.code == f"execution_blocked_{expected_gate}"
    toolset.close()


def test_execute_decision_idempotency_gate_denial_on_a_real_repeat_call(stub_server: str) -> None:
    """`"idempotency"` isn't a fixed fixture like the other two gates -- it's real per-decision
    state: any decision id that already delivered blocks a second, un-forced call."""
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    hooks = build_algenta_governance_hooks()
    decision_id = "decision-idempotency-check"

    first_generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"decision_id": decision_id, "webhook_url": WEBHOOK_URL}), text_reply("done")]
    )
    first_agent = Agent(chat_generator=first_generator, tools=toolset, hooks=hooks)
    first_agent.warm_up()
    first_agent.run(messages=[])  # delivers decision_id for real

    second_generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"decision_id": decision_id, "webhook_url": WEBHOOK_URL})]
    )
    second_agent = Agent(chat_generator=second_generator, tools=toolset, hooks=hooks)
    second_agent.warm_up()

    with pytest.raises(AlgentaToolDenied) as exc_info:
        second_agent.run(messages=[])

    assert exc_info.value.gate == "idempotency"
    toolset.close()


def test_smuggled_force_never_reaches_the_real_server_over_the_real_wire(stub_server: str) -> None:
    """Same proof as `test_never_model_facing.py`, but end to end against the real stub server
    (not a fake registry): `force` really is on the real MCP tool's real advertised schema (see
    `stub_server.execute_decision`'s docstring), and this proves the call-time scrub strips it
    before the real wire call regardless -- a smuggled `force=True` on an already-delivered
    decision id still gets blocked by the real idempotency gate, because the real underlying call
    only ever sees the scrubbed `force=False`.
    """
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in toolset if t.name == "execute_decision")
    assert "force" not in execute_decision.parameters.get("properties", {})

    decision_id = "decision-force-scrub-check"
    execute_decision.invoke(decision_id=decision_id, webhook_url=WEBHOOK_URL)  # first, real delivery

    # Simulates a model/caller that still supplies `force=True` despite the schema not advertising
    # it -- the call-time scrub (not schema validation) is what has to stop it from reaching the
    # real wire call.
    raw_result = execute_decision.invoke(decision_id=decision_id, webhook_url=WEBHOOK_URL, force=True)
    outcome = extract_execution_outcome_from_tool_result(raw_result)
    assert isinstance(outcome, ExecutionBlocked)
    assert outcome.gate == "idempotency"
    toolset.close()


def test_a_blows_up_tool_error_is_wrapped_and_swallowed_by_default(stub_server: str) -> None:
    """Backs the exact claim `haystack_algenta.exceptions`/`toolset`/`hooks` all document: a real
    server-side exception is unconditionally rewrapped into `ToolInvocationError`, and with a real
    `Agent`'s documented default (`raise_on_tool_invocation_failure=False`), silently swallowed
    into an ordinary error-flagged tool-result message -- `agent.run()` returns normally, nothing
    is raised.
    """
    mcp_toolset = MCPToolset(server_info=StreamableHttpServerInfo(url=stub_server), tool_names=["blows_up"])
    mcp_toolset.warm_up()
    # `profile="full"` -- `blows_up` isn't a contract tool at all, so any named profile would
    # filter it out entirely (as `test_profile_filtering.py` proves deliberately).
    toolset = create_algenta_tools(mcp_toolset=mcp_toolset, profile="full")
    generator = ScriptedChatGenerator([tool_call_reply("blows_up", {"decision_id": "decision-x"})])
    agent = Agent(chat_generator=generator, tools=toolset, raise_on_tool_invocation_failure=False)
    agent.warm_up()

    result = agent.run(messages=[])  # must not raise

    tool_result_messages = [m for m in result["messages"] if m.tool_call_result is not None]
    assert tool_result_messages
    assert tool_result_messages[0].tool_call_result.error is True
    mcp_toolset.close()


def test_a_blows_up_tool_error_raises_toolinvocationerror_when_configured_to(stub_server: str) -> None:
    mcp_toolset = MCPToolset(server_info=StreamableHttpServerInfo(url=stub_server), tool_names=["blows_up"])
    mcp_toolset.warm_up()
    toolset = create_algenta_tools(mcp_toolset=mcp_toolset, profile="full")
    generator = ScriptedChatGenerator([tool_call_reply("blows_up", {"decision_id": "decision-x"})])
    agent = Agent(chat_generator=generator, tools=toolset, raise_on_tool_invocation_failure=True)
    agent.warm_up()

    with pytest.raises(ToolInvocationError):
        agent.run(messages=[])
    mcp_toolset.close()
