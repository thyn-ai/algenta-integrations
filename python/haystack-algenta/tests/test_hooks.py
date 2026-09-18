"""Unit tests for `haystack_algenta.hooks.GovernedReceiptHook` against a real (but hand-built,
no-agent) `haystack.components.agents.state.state.State` -- no network, no `Agent` run loop.

`tests/test_toolset_scenarios.py` covers the full, real `Agent`-driven equivalent, including the
real stub-server wire round trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from haystack.components.agents.state.state import State
from haystack.dataclasses import ChatMessage, ToolCall
from haystack_algenta import AlgentaToolDenied, build_algenta_governance_hooks
from haystack_algenta.hooks import GovernedReceiptHook


def _state_with_tool_result(tool_name: str, result: object, *, error: bool = False) -> State:
    tool_call = ToolCall(id="c1", tool_name=tool_name, arguments={})
    payload = result if isinstance(result, str) else json.dumps(result)
    message = ChatMessage.from_tool(tool_result=payload, origin=tool_call, error=error)
    return State(schema={"messages": {"type": list[ChatMessage]}}, data={"messages": [message]})


def _receipt(**overrides: object) -> dict:
    base = {
        "decision_id": "decision-1",
        "webhook_url": "https://example.test/hook",
        "execution_status": "delivered",
        "safety_overridden": False,
    }
    base.update(overrides)
    return base


def _blocked(gate: str, **overrides: object) -> dict:
    error = {"code": f"execution_blocked_{gate}", "gate": gate, "message": f"blocked by {gate}", "override_hint": None}
    error.update(overrides)
    return {"error": error}


def test_successful_receipt_does_not_raise() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("execute_decision", _receipt())
    hook.run(state)  # must not raise


def test_failed_delivery_receipt_does_not_raise() -> None:
    # execution_status="failed" is a webhook-delivery outcome, not a policy denial.
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("execute_decision", _receipt(execution_status="failed", response_code=None))
    hook.run(state)  # must not raise


def test_non_envelope_result_is_skipped_regardless_of_tool_name() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("get_contract", {"capabilities": ["query"], "engine_version": "1.4.0"})
    hook.run(state)  # must not raise -- doesn't parse as a receipt or a denial at all


def test_log_decisions_own_result_shape_is_skipped() -> None:
    # log_decision's real result has decision_id but no webhook_url/execution_status -- must not
    # be mistaken for an execute_decision outcome.
    hook = GovernedReceiptHook()
    state = _state_with_tool_result(
        "log_decision", {"decision_id": "decision-1", "chosen_action": "hold", "note": None}
    )
    hook.run(state)  # must not raise


@pytest.mark.parametrize("gate", ["idempotency", "confidence", "risk_floor"])
def test_each_real_named_gate_raises_tool_denied(gate: str) -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("execute_decision", _blocked(gate))
    with pytest.raises(AlgentaToolDenied) as exc_info:
        hook.run(state)
    assert exc_info.value.gate == gate
    assert exc_info.value.blocked is not None
    assert exc_info.value.blocked.code == f"execution_blocked_{gate}"
    assert gate in str(exc_info.value)


def test_denial_message_includes_the_override_hint_when_present() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result(
        "execute_decision", _blocked("confidence", override_hint="Set override_safety=true to bypass.")
    )
    with pytest.raises(AlgentaToolDenied) as exc_info:
        hook.run(state)
    assert "Set override_safety=true to bypass." in str(exc_info.value)
    assert exc_info.value.override_hint == "Set override_safety=true to bypass."


def test_hook_is_restricted_to_after_tool() -> None:
    assert GovernedReceiptHook.allowed_hook_points == ("after_tool",)


def test_run_async_applies_the_same_mapping() -> None:
    # No `pytest-asyncio` in this package's test suite (see `tests/conftest.py`'s docstring) --
    # `Agent.run_async()` itself is real and worth covering, so drive the coroutine with a plain
    # `asyncio.run` from an ordinary sync test instead of adding a whole plugin for one test.
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("execute_decision", _blocked("idempotency"))
    with pytest.raises(AlgentaToolDenied):
        asyncio.run(hook.run_async(state))


# --- build_algenta_governance_hooks ---------------------------------------------------------------


def test_build_algenta_governance_hooks_registers_only_after_tool() -> None:
    hooks = build_algenta_governance_hooks()
    assert set(hooks) == {"after_tool"}
    assert len(hooks["after_tool"]) == 1
    assert isinstance(hooks["after_tool"][0], GovernedReceiptHook)


def test_build_algenta_governance_hooks_has_no_before_tool_pre_call_gate() -> None:
    # There is nothing left to confirm pre-call: execute_decision never comes back pending, it is
    # a same-call success or a same-call named-gate denial. See haystack_algenta.hooks's docstring.
    hooks = build_algenta_governance_hooks()
    assert "before_tool" not in hooks
