"""Tests for `recipes/approval_gated_agent.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

import json
from typing import Any

from langchain_algenta import create_algenta_tools

from recipes.approval_gated_agent import run_approval_gated_agent


async def _always_approve(_decision_record: dict[str, Any]) -> bool:
    return True


async def _always_reject(_decision_record: dict[str, Any]) -> bool:
    return False


async def test_an_approved_decision_executes_and_returns_a_real_receipt(stub_server: str) -> None:
    result = await run_approval_gated_agent(
        stub_server,
        scenario="expand-eu",
        chosen_action="expand-eu",
        webhook_url="https://example.com/hook",
        approval_gate=_always_approve,
    )

    assert result["approved"] is True
    assert result["decision_id"] == "decision-expand-eu"
    receipt = result["receipt"]
    assert receipt is not None
    assert receipt.decision_id == "decision-expand-eu"
    assert receipt.is_delivered()


async def test_a_rejected_decision_is_never_executed(stub_server: str) -> None:
    result = await run_approval_gated_agent(
        stub_server,
        scenario="expand-eu",
        chosen_action="expand-eu",
        webhook_url="https://example.com/hook",
        approval_gate=_always_reject,
    )

    assert result["approved"] is False
    assert result["receipt"] is None

    # Proof nothing executed: the engine's delivery counter is still zero.
    ops_tools = await create_algenta_tools(base_url=stub_server, profile="full")
    diagnostics = next(tool for tool in ops_tools if tool.name == "_test_diagnostics")
    raw = await diagnostics.ainvoke({})
    assert json.loads(raw[0]["text"])["delivered_count"] == 0


async def test_the_model_facing_tool_list_has_no_execute_decision(stub_server: str) -> None:
    govern_tools = await create_algenta_tools(base_url=stub_server, profile="govern")
    assert "execute_decision" not in {tool.name for tool in govern_tools}
