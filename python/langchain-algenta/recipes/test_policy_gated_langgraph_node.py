"""Tests for `recipes/policy_gated_langgraph_node.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

import pytest
from tests.stub_server import (
    BELOW_RISK_FLOOR_DECISION_ID,
    FAILED_DELIVERY_DECISION_ID,
    LOW_CONFIDENCE_DECISION_ID,
)

from recipes.policy_gated_langgraph_node import run_policy_gated_node


async def test_a_delivered_execution_routes_to_accept(stub_server: str) -> None:
    state = await run_policy_gated_node(stub_server, decision_id="decision-1", webhook_url="https://example.com/hook")
    assert state["outcome"] == "delivered"
    assert state["verdict"] == "accepted"


async def test_a_failed_webhook_delivery_routes_to_quarantine_not_review(stub_server: str) -> None:
    state = await run_policy_gated_node(
        stub_server, decision_id=FAILED_DELIVERY_DECISION_ID, webhook_url="https://example.com/hook"
    )
    assert state["outcome"] == "failed"
    assert state["verdict"] == "quarantined-delivery-failure"


@pytest.mark.parametrize(
    ("decision_id", "gate"),
    [(LOW_CONFIDENCE_DECISION_ID, "confidence"), (BELOW_RISK_FLOOR_DECISION_ID, "risk_floor")],
)
async def test_a_policy_denial_routes_to_review_with_the_named_gate(
    stub_server: str, decision_id: str, gate: str
) -> None:
    state = await run_policy_gated_node(stub_server, decision_id=decision_id, webhook_url="https://example.com/hook")
    assert state["outcome"] == "blocked"
    assert state["gate"] == gate
    assert state["verdict"] == f"needs-review:{gate}"
