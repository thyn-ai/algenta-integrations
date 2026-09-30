"""Golden-path tests for the governed support-triage example.

Every test exercises the real LangGraph -> langchain-algenta -> MCP wire -> stub
server round trip with deterministic, injected approval gates. No network, no
real LLM, no proprietary service is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_algenta import ExecutionReceipt, create_algenta_tools
from support_triage.agent import run_triage


async def _always_approve(_decision_record: dict[str, Any]) -> bool:
    return True


async def _always_reject(_decision_record: dict[str, Any]) -> bool:
    return False


async def _approve_only_refunds(decision_record: dict[str, Any]) -> bool:
    return decision_record["chosen_action"] == "refund"


@pytest.fixture
def ticket_refund() -> dict[str, Any]:
    return {
        "id": "T-refund",
        "scenario": "customer received a damaged widget and wants a refund",
        "action": "refund",
    }


@pytest.fixture
def ticket_escalate() -> dict[str, Any]:
    return {
        "id": "T-escalate",
        "scenario": "enterprise account requests expedited onboarding",
        "action": "escalate",
    }


async def test_approved_ticket_executes_and_persists_receipt(
    stub_server: str, tmp_path: Path, ticket_refund: dict[str, Any]
) -> None:
    final_state = await run_triage(
        stub_server,
        [ticket_refund],
        approval_gate=_always_approve,
        receipts_dir=tmp_path,
    )

    assert final_state["done"] is True
    assert len(final_state["persisted_paths"]) == 1

    receipt_path = Path(final_state["persisted_paths"][0])
    assert receipt_path.exists()
    receipt = ExecutionReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.decision_id == "decision-refund"
    assert receipt.execution_status == "delivered"
    assert receipt.is_delivered()


async def test_rejected_ticket_is_skipped_and_not_persisted(
    stub_server: str, tmp_path: Path, ticket_refund: dict[str, Any]
) -> None:
    final_state = await run_triage(
        stub_server,
        [ticket_refund],
        approval_gate=_always_reject,
        receipts_dir=tmp_path,
    )

    assert final_state["done"] is True
    assert final_state["persisted_paths"] == []
    assert not list(tmp_path.iterdir())

    # Proof nothing executed: the engine's delivery counter is still zero.
    ops_tools = await create_algenta_tools(base_url=stub_server, profile="full")
    diagnostics = next(tool for tool in ops_tools if tool.name == "_test_diagnostics")
    raw = await diagnostics.ainvoke({})
    assert json.loads(raw[0]["text"])["delivered_count"] == 0


async def test_mixed_queue_respects_per_ticket_approval(
    stub_server: str,
    tmp_path: Path,
    ticket_refund: dict[str, Any],
    ticket_escalate: dict[str, Any],
) -> None:
    final_state = await run_triage(
        stub_server,
        [ticket_refund, ticket_escalate],
        approval_gate=_approve_only_refunds,
        receipts_dir=tmp_path,
    )

    assert final_state["done"] is True
    assert len(final_state["persisted_paths"]) == 1

    receipt = ExecutionReceipt.model_validate_json(
        Path(final_state["persisted_paths"][0]).read_text()
    )
    assert receipt.decision_id == "decision-refund"


async def test_govern_profile_does_not_expose_execute_decision(stub_server: str) -> None:
    govern_tools = await create_algenta_tools(base_url=stub_server, profile="govern")
    assert "execute_decision" not in {tool.name for tool in govern_tools}


async def test_observe_profile_does_not_expose_govern_or_execute_tools(stub_server: str) -> None:
    observe_tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    names = {tool.name for tool in observe_tools}
    assert names == {"get_contract", "query_data", "simulate", "recommend"}
