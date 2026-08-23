"""End-to-end scenarios against the real stub Algenta MCP server, driven through a real
`haystack.components.agents.Agent` run loop (via `ScriptedChatGenerator`, not a mock of anything
in `haystack_algenta` or `haystack`/`mcp-haystack` themselves).

Every scenario below exercises the real `haystack_algenta.create_algenta_tools` -> real
`haystack_integrations.tools.mcp.MCPToolset` -> real `mcp` client -> real wire ->
`tests/stub_server.py` round trip, and the real Haystack `Agent` step loop (the `ConfirmationHook`
pre-call gate, real tool invocation, the `after_tool` `GovernedReceiptHook`, exception
propagation) -- nothing about the approval gate, the denial, the failure, or the never-model-facing
scrub is asserted by inspecting `haystack_algenta`'s internals directly; each is proven by
actually running an agent and observing what came back or what was raised.
"""

from __future__ import annotations

from typing import Any

import pytest
from haystack.components.agents import Agent
from haystack.hooks.human_in_the_loop import AlwaysAskPolicy, BlockingConfirmationStrategy, ConfirmationHook, ConfirmationUIResult
from haystack.tools.errors import ToolInvocationError
from haystack_integrations.tools.mcp import MCPToolset, StreamableHttpServerInfo

from haystack_algenta import (
    AlgentaApprovalStillPending,
    AlgentaToolDenied,
    build_algenta_governance_hooks,
    create_algenta_tools,
)
from haystack_algenta.receipts import extract_receipt_from_tool_result

from .fake_chat_generator import ScriptedChatGenerator, text_reply, tool_call_reply
from .stub_server import PENDING_PLAN_HASH, REJECTED_PLAN_HASH


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
    def from_dict(cls, data: dict[str, Any]) -> "_ScriptedUI":
        return cls(**data.get("init_parameters", {}))


def _approve_plan_out_of_band(base_url: str, plan_hash: str) -> None:
    """Simulate "a human approved this plan via the engine's real HTTP endpoint" by calling the
    stub server's test-only administrative tool directly -- deliberately *not* through
    `create_algenta_tools` (that tool isn't part of the real contract and would never be exposed
    to a model), mirroring exactly how a real out-of-band approval call would bypass the
    model-facing tool surface entirely.
    """
    admin_toolset = MCPToolset(server_info=StreamableHttpServerInfo(url=base_url), tool_names=["_test_approve_plan"])
    admin_toolset.warm_up()
    approve_tool = next(t for t in admin_toolset.get_selectable_tools() if t.name == "_test_approve_plan")
    approve_tool.invoke(plan_hash=plan_hash)
    admin_toolset.close()


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
    toolset = create_algenta_tools(base_url=stub_server, profile="observe")
    query_data = next(t for t in toolset if t.name == "query_data")
    raw_result = query_data.invoke(dataset="widgets")
    receipt = extract_receipt_from_tool_result(raw_result)
    assert receipt is not None
    assert receipt.status == "ok"
    assert receipt.result == {"dataset": "widgets", "rows": [{"value": 1}, {"value": 2}]}
    toolset.close()


def test_execute_decision_pauses_for_approval_before_the_real_call(stub_server: str) -> None:
    """`ConfirmationHook` + `BlockingConfirmationStrategy` + a UI that rejects: the real MCP
    server is genuinely never contacted for this call."""
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    ui = _ScriptedUI(action="reject")
    hook = ConfirmationHook(
        confirmation_strategies={
            "execute_decision": BlockingConfirmationStrategy(confirmation_policy=AlwaysAskPolicy(), confirmation_ui=ui)
        }
    )
    generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1"})]
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


def test_execute_decision_still_pending_after_confirmation_gate_raises(stub_server: str) -> None:
    """The honest, verified finding this package's design is built on: Haystack's `ConfirmationHook`
    pre-call gate and the engine's own `approval_state` are orthogonal. Confirming the *call*
    doesn't retroactively record the *plan's* out-of-band policy approval on the engine, so the
    receipt still comes back `"pending"` -- and `GovernedReceiptHook` (an `after_tool` hook) turns
    that into a fail-closed `AlgentaApprovalStillPending` that propagates out of `agent.run()`
    completely unmodified.
    """
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    governance = build_algenta_governance_hooks(confirmation_ui=_ScriptedUI(action="confirm"))
    generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1"})]
    )
    agent = Agent(chat_generator=generator, tools=toolset, hooks=governance.as_agent_hooks())
    agent.warm_up()

    with pytest.raises(AlgentaApprovalStillPending) as exc_info:
        agent.run(messages=[])

    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.plan_hash == PENDING_PLAN_HASH
    assert exc_info.value.receipt.approval_state == "pending"
    toolset.close()


def test_execute_decision_denied_after_confirmation_gate_raises(stub_server: str) -> None:
    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    governance = build_algenta_governance_hooks(confirmation_ui=_ScriptedUI(action="confirm"))
    generator = ScriptedChatGenerator(
        [tool_call_reply("execute_decision", {"plan_hash": REJECTED_PLAN_HASH, "idempotency_key": "idem-1"})]
    )
    agent = Agent(chat_generator=generator, tools=toolset, hooks=governance.as_agent_hooks())
    agent.warm_up()

    with pytest.raises(AlgentaToolDenied) as exc_info:
        agent.run(messages=[])

    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.code == "stale_plan"
    assert "stale_plan" in str(exc_info.value)
    toolset.close()


def test_execute_decision_full_success_round_trip_after_out_of_band_approval(stub_server: str) -> None:
    plan_hash = "plan-needs-approval-full-success"
    _approve_plan_out_of_band(stub_server, plan_hash)

    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    governance = build_algenta_governance_hooks(confirmation_ui=_ScriptedUI(action="confirm"))
    generator = ScriptedChatGenerator(
        [
            tool_call_reply("execute_decision", {"plan_hash": plan_hash, "idempotency_key": "idem-1"}),
            text_reply("done"),
        ]
    )
    agent = Agent(chat_generator=generator, tools=toolset, hooks=governance.as_agent_hooks())
    agent.warm_up()

    result = agent.run(messages=[])
    assert result["messages"][-1].text == "done"
    tool_result_messages = [m for m in result["messages"] if m.tool_call_result is not None]
    assert tool_result_messages, "expected the real tool call to have actually happened"
    assert tool_result_messages[0].tool_call_result.error is False
    toolset.close()


def test_smuggled_force_never_reaches_the_real_server_over_the_real_wire(stub_server: str) -> None:
    """Same proof as `test_never_model_facing.py`, but end to end against the real stub server
    (not a fake registry) -- `force` really is on the real MCP tool's real advertised schema (see
    `stub_server.execute_decision`'s docstring), and this proves the call-time scrub strips it
    before the real wire call regardless.
    """
    plan_hash = "plan-needs-approval-force-scrub"
    _approve_plan_out_of_band(stub_server, plan_hash)

    toolset = create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in toolset if t.name == "execute_decision")
    assert "force" not in execute_decision.parameters.get("properties", {})

    # Simulates a model/caller that still supplies `force=True` despite the schema not
    # advertising it -- the call-time scrub (not schema validation) is what has to stop it.
    raw_result = execute_decision.invoke(plan_hash=plan_hash, idempotency_key="idem-1", force=True)
    receipt = extract_receipt_from_tool_result(raw_result)
    assert receipt is not None
    # `stub_server.execute_decision` echoes the `force` value it actually received in
    # `result.forced` -- proving the real underlying MCP call saw `force=False` (its own
    # default), never the model-smuggled `force=True`.
    assert receipt.result["forced"] is False
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
    generator = ScriptedChatGenerator([tool_call_reply("blows_up", {"plan_hash": "plan-x"})])
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
    generator = ScriptedChatGenerator([tool_call_reply("blows_up", {"plan_hash": "plan-x"})])
    agent = Agent(chat_generator=generator, tools=toolset, raise_on_tool_invocation_failure=True)
    agent.warm_up()

    with pytest.raises(ToolInvocationError):
        agent.run(messages=[])
    mcp_toolset.close()
