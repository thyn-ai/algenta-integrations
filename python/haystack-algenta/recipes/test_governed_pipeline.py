"""Tests for `recipes/governed_pipeline.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

import pytest
from haystack_algenta import AlgentaToolDenied, ExecutionReceipt

from recipes.governed_pipeline import build_governed_pipeline, run_governed_pipeline

WEBHOOK_URL = "https://example.test/hook"


@pytest.fixture
def governed_pipeline(stub_server: str):
    """Build the recipe pipeline against the stub server and tear down its toolsets after the test."""
    pipeline, observe_toolset, execute_toolset = build_governed_pipeline(base_url=stub_server)
    yield pipeline
    observe_toolset.close()
    execute_toolset.close()


def test_pipeline_returns_recommendation_and_receipt(governed_pipeline) -> None:
    result = run_governed_pipeline(
        governed_pipeline,
        scenario="scenario-alpha",
        decision_id="decision-pipeline-success",
        webhook_url=WEBHOOK_URL,
    )

    recommendation = result["recommend"]["recommendation"]
    assert recommendation["recommended_action"] == "hold"
    assert recommendation["confidence"] == 0.87

    receipt = result["execute_decision"]["receipt"]
    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.decision_id == "decision-pipeline-success"
    assert receipt.is_delivered()


@pytest.mark.parametrize(
    ("decision_id", "expected_gate"),
    [
        ("decision-low-confidence", "confidence"),
        ("decision-risky", "risk_floor"),
    ],
)
def test_pipeline_raises_tool_denied_on_named_gate(
    governed_pipeline, decision_id: str, expected_gate: str
) -> None:
    with pytest.raises(AlgentaToolDenied) as exc_info:
        run_governed_pipeline(
            governed_pipeline,
            scenario="scenario-denied",
            decision_id=decision_id,
            webhook_url=WEBHOOK_URL,
        )

    assert exc_info.value.gate == expected_gate
    assert exc_info.value.blocked is not None
    assert exc_info.value.blocked.code == f"execution_blocked_{expected_gate}"


def test_pipeline_raises_tool_denied_on_repeat_idempotency_gate(governed_pipeline) -> None:
    # First call delivers the decision.
    run_governed_pipeline(
        governed_pipeline,
        scenario="scenario-alpha",
        decision_id="decision-pipeline-idempotency",
        webhook_url=WEBHOOK_URL,
    )

    # Second, un-forced call is blocked by the idempotency gate.
    with pytest.raises(AlgentaToolDenied) as exc_info:
        run_governed_pipeline(
            governed_pipeline,
            scenario="scenario-alpha",
            decision_id="decision-pipeline-idempotency",
            webhook_url=WEBHOOK_URL,
        )

    assert exc_info.value.gate == "idempotency"
    assert exc_info.value.blocked is not None
    assert exc_info.value.blocked.code == "execution_blocked_idempotency"
