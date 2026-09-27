"""Recipe 5 tests: a policy denial mid-saga compensates every delivered step in reverse, and a
clean saga completes with no compensations.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.saga_with_policy_gates import PolicyGatedSagaWorkflow, SagaReport, SagaStep
from temporalio.client import WorkflowFailureError

from .helpers import workflow_worker

HOLD = SagaStep(action="hold", webhook_url="https://ops.example.com/hooks/hold")
RISKY = SagaStep(action="risky", webhook_url="https://ops.example.com/hooks/yolo")
SETTLE = SagaStep(action="settle", webhook_url="https://ops.example.com/hooks/settle")


async def test_denial_triggers_reverse_order_compensation(temporal_env, stub_server) -> None:
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env, [PolicyGatedSagaWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        report = await asyncio.wait_for(client.execute_workflow(
            PolicyGatedSagaWorkflow.run,
            [HOLD, SETTLE, RISKY],
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=SagaReport,
        ), timeout=90)

    assert report.status == "compensated"
    assert report.denial_gate == "risk_floor"
    assert report.denial_message is not None
    # Forward steps 1-2 delivered; step 3 (risk_floor) never did.
    assert report.executed_decision_ids == ["decision-hold", "decision-settle"]
    assert engine.delivered_decision_ids == {"decision-hold", "decision-settle"}
    # Compensations ran in reverse, and are themselves recorded in decision memory.
    assert report.compensated_decision_ids == ["decision-compensate-settle", "decision-compensate-hold"]
    compensation_notes = [d["note"] for d in engine.logged_decisions if d["note"]]
    assert any("compensates decision-settle" in note for note in compensation_notes)
    assert any("decision-risky was denied" in note for note in compensation_notes)


async def test_clean_saga_completes_without_compensations(temporal_env, stub_server) -> None:
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env, [PolicyGatedSagaWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        report = await asyncio.wait_for(client.execute_workflow(
            PolicyGatedSagaWorkflow.run,
            [HOLD, SETTLE],
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=SagaReport,
        ), timeout=90)
    assert report.status == "completed"
    assert report.executed_decision_ids == ["decision-hold", "decision-settle"]
    assert report.compensated_decision_ids == []
    assert report.denial_gate is None
    assert engine.delivered_decision_ids == {"decision-hold", "decision-settle"}


async def test_non_denial_failures_fail_the_saga_loudly(temporal_env, stub_server) -> None:
    """A failure that isn't a policy denial must not be compensated around: the saga fails.

    Here the worker runs under `govern`, so `execute_decision` is refused client-side with a
    typed, non-retryable *profile* error -- which carries no denial body. The saga's
    `denial_from_activity_error` correctly finds nothing, and the workflow re-raises instead of
    compensating around an unknown state.
    """
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env, [PolicyGatedSagaWorkflow], base_url=base_url, profile="govern"
    ) as (client, task_queue):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await asyncio.wait_for(client.execute_workflow(
                PolicyGatedSagaWorkflow.run,
                [HOLD],
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            ), timeout=90)
    assert _chain_contains_error_type(exc_info.value, "algenta_tool_denied_outside_profile")
    # Nothing delivered, and no compensation was recorded for a non-denial failure.
    assert engine.delivered_decision_ids == set()
    assert not any(d["chosen_action"].startswith("compensate-") for d in engine.logged_decisions)


def _chain_contains_error_type(error: BaseException, error_type: str) -> bool:
    from temporalio.exceptions import ApplicationError as _AppError

    current: BaseException | None = error
    while current is not None:
        if isinstance(current, _AppError) and current.type == error_type:
            return True
        current = current.__cause__
    return False
