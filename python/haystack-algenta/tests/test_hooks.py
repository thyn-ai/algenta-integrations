"""Unit tests for `haystack_algenta.hooks.GovernedReceiptHook` against a real (but hand-built,
no-agent) `haystack.components.agents.state.state.State` -- no network, no `Agent` run loop.

`tests/test_toolset_scenarios.py` covers the full, real `Agent`-driven equivalent, including the
real `ConfirmationHook` pre-call gate and the real stub-server wire round trip.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from haystack.components.agents.state.state import State
from haystack.dataclasses import ChatMessage, ToolCall
from haystack.hooks.human_in_the_loop import AlwaysAskPolicy, ConfirmationHook, NeverAskPolicy

from haystack_algenta import (
    AlgentaApprovalStillPending,
    AlgentaGovernanceHooks,
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
    build_algenta_governance_hooks,
    default_confirmation_hook,
)
from haystack_algenta.hooks import GovernedReceiptHook


def _state_with_tool_result(tool_name: str, result: object, *, error: bool = False) -> State:
    tool_call = ToolCall(id="c1", tool_name=tool_name, arguments={})
    payload = result if isinstance(result, str) else json.dumps(result)
    message = ChatMessage.from_tool(tool_result=payload, origin=tool_call, error=error)
    return State(schema={"messages": {"type": list[ChatMessage]}}, data={"messages": [message]})


def _receipt(**overrides: object) -> dict:
    base = {"status": "ok", "code": "ok", "approval_state": "none", "result": {}}
    base.update(overrides)
    return base


def test_successful_receipt_does_not_raise() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("query_data", _receipt())
    hook.run(state)  # must not raise


def test_non_envelope_result_is_skipped_regardless_of_tool_name() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("get_contract", {"capabilities": ["query"], "engine_version": "1.4.0"})
    hook.run(state)  # must not raise -- doesn't parse as a receipt at all


def test_pending_receipt_raises_approval_still_pending() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result(
        "execute_decision", _receipt(approval_state="pending", plan_hash="plan-x")
    )
    with pytest.raises(AlgentaApprovalStillPending) as exc_info:
        hook.run(state)
    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.plan_hash == "plan-x"


def test_rejected_receipt_raises_tool_denied() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result(
        "execute_decision",
        _receipt(status="error", code="stale_plan", approval_state="rejected", plan_hash="plan-y"),
    )
    with pytest.raises(AlgentaToolDenied) as exc_info:
        hook.run(state)
    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.code == "stale_plan"
    assert "stale_plan" in str(exc_info.value)


def test_named_policy_gate_code_raises_tool_denied_even_with_approval_state_none() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result(
        "execute_decision", _receipt(status="error", code="plan_hash_mismatch", approval_state="none")
    )
    with pytest.raises(AlgentaToolDenied):
        hook.run(state)


def test_generic_failure_raises_tool_execution_failed() -> None:
    hook = GovernedReceiptHook()
    state = _state_with_tool_result(
        "query_data", _receipt(status="error", code="upstream_timeout", approval_state="none")
    )
    with pytest.raises(AlgentaToolExecutionFailed):
        hook.run(state)


def test_hook_is_restricted_to_after_tool() -> None:
    assert GovernedReceiptHook.allowed_hook_points == ("after_tool",)


def test_run_async_applies_the_same_mapping() -> None:
    # No `pytest-asyncio` in this package's test suite (see `tests/conftest.py`'s docstring) --
    # `Agent.run_async()` itself is real and worth covering, so drive the coroutine with a plain
    # `asyncio.run` from an ordinary sync test instead of adding a whole plugin for one test.
    hook = GovernedReceiptHook()
    state = _state_with_tool_result("execute_decision", _receipt(approval_state="pending", plan_hash="plan-async"))
    with pytest.raises(AlgentaApprovalStillPending):
        asyncio.run(hook.run_async(state))


# --- default_confirmation_hook / build_algenta_governance_hooks composition ----------------------


def test_default_confirmation_hook_gates_only_the_requested_tool_names() -> None:
    hook = default_confirmation_hook(confirmation_ui=object(), confirmation_policy=NeverAskPolicy())
    assert isinstance(hook, ConfirmationHook)
    assert "execute_decision" in hook.confirmation_strategies


def test_default_confirmation_hook_defaults_to_always_ask() -> None:
    hook = default_confirmation_hook(confirmation_ui=object())
    strategy = hook.confirmation_strategies["execute_decision"]
    assert isinstance(strategy.confirmation_policy, AlwaysAskPolicy)


def test_build_algenta_governance_hooks_without_confirmation_ui_only_registers_after_tool() -> None:
    governance = build_algenta_governance_hooks()
    assert governance.before_tool == []
    assert len(governance.after_tool) == 1
    assert isinstance(governance.after_tool[0], GovernedReceiptHook)
    agent_hooks = governance.as_agent_hooks()
    assert "before_tool" not in agent_hooks
    assert "after_tool" in agent_hooks


def test_build_algenta_governance_hooks_with_confirmation_ui_registers_both() -> None:
    governance = build_algenta_governance_hooks(confirmation_ui=object())
    assert len(governance.before_tool) == 1
    assert isinstance(governance.before_tool[0], ConfirmationHook)
    agent_hooks = governance.as_agent_hooks()
    assert agent_hooks["before_tool"] == governance.before_tool
    assert agent_hooks["after_tool"] == governance.after_tool


def test_algenta_governance_hooks_container_is_independently_constructible() -> None:
    governance = AlgentaGovernanceHooks()
    assert governance.as_agent_hooks() == {}
