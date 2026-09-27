"""Recipe 3 tests: the engine's idempotency gate turns a retried/duplicated execution into a
deduplicated success instead of a double delivery or a failure.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.idempotent_activity_receipt_dedup import (
    IdempotentDecisionWorkflow,
    IdempotentExecutionResult,
)
from temporal_algenta.types import GovernedDecisionInput

from .helpers import with_session_resilient, workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")

INPUT = GovernedDecisionInput(action="hold", webhook_url="https://ops.example.com/hooks/hold")


async def test_first_delivery_is_not_deduplicated(temporal_env, stub_server_mod) -> None:
    base_url, engine = stub_server_mod
    async with workflow_worker(
        temporal_env, [IdempotentDecisionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        result = await asyncio.wait_for(client.execute_workflow(
            IdempotentDecisionWorkflow.run,
            INPUT,
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=IdempotentExecutionResult,
        ), timeout=150)
    assert result.decision_id == "decision-hold"
    assert result.delivered is True
    assert result.deduplicated is False
    assert result.response_code == 200
    assert engine.delivered_decision_ids == {"decision-hold"}


async def test_prior_delivery_is_reported_as_deduplicated_success(temporal_env, stub_server_mod) -> None:
    """Simulate the crash window: a previous attempt delivered the decision but its completion
    never made it back (here: a direct engine call standing in for that lost attempt). The
    workflow's retry re-attempts the delivery, the idempotency gate blocks it, and the workflow
    reports a deduplicated success -- not a double delivery, not a failure."""
    base_url, engine = stub_server_mod
    prior = await with_session_resilient(
        base_url=base_url,
        profile="execute",
        fn=lambda c: c.call_tool(
            "execute_decision", {"decision_id": "decision-hold", "webhook_url": INPUT.webhook_url}
        ),
    )
    assert prior["execution_status"] == "delivered"

    async with workflow_worker(
        temporal_env, [IdempotentDecisionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        result = await asyncio.wait_for(client.execute_workflow(
            IdempotentDecisionWorkflow.run,
            INPUT,
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=IdempotentExecutionResult,
        ), timeout=150)

    assert result.delivered is True
    assert result.deduplicated is True
    assert result.response_code is None
    # Exactly one delivery ever happened, across both the "lost" attempt and this run.
    assert engine.delivered_decision_ids == {"decision-hold"}
    assert engine.execute_attempts["decision-hold"] == 2  # one delivered, one gate-blocked


async def test_other_denials_are_not_swallowed_by_the_dedup_mapping(temporal_env, stub_server_mod) -> None:
    from temporalio.client import WorkflowFailureError

    base_url, _engine = stub_server_mod
    async with workflow_worker(
        temporal_env, [IdempotentDecisionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(client.execute_workflow(
                IdempotentDecisionWorkflow.run,
                GovernedDecisionInput(action="risky", webhook_url="https://ops.example.com/hooks/yolo"),
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            ), timeout=150)
