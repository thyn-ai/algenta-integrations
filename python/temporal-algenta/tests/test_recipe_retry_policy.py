"""Recipe 6 tests: transient engine errors are retried by the policy; policy denials fail on
the first attempt with the structured denial intact all the way to the client.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.demo_engine import (
    BELOW_RISK_FLOOR_DECISION_ID,
    FLAKY_DATASET,
    FLAKY_DATASET_FAILURES,
)
from recipes.retry_policy_structured_denials import (
    DenialFailsFastWorkflow,
    TransientTolerantQueryWorkflow,
)
from temporal_algenta.types import denial_from_activity_error
from temporalio.client import WorkflowFailureError

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")

WORKFLOWS = [TransientTolerantQueryWorkflow, DenialFailsFastWorkflow]


async def test_transient_engine_errors_are_retried_until_success(temporal_env, stub_server_mod) -> None:
    base_url, engine = stub_server_mod
    async with workflow_worker(temporal_env, WORKFLOWS, base_url=base_url, profile="execute") as (
        client,
        task_queue,
    ):
        result = await asyncio.wait_for(client.execute_workflow(
            TransientTolerantQueryWorkflow.run,
            FLAKY_DATASET,
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
        ), timeout=150)
    assert result["dataset"] == FLAKY_DATASET
    assert result["rows"] == [{"value": 1}, {"value": 2}]
    # Exactly the documented number of flakes, then one successful attempt.
    assert engine.query_attempts[FLAKY_DATASET] == FLAKY_DATASET_FAILURES + 1


async def test_policy_denial_fails_fast_with_zero_wasted_retries(temporal_env, stub_server_mod) -> None:
    base_url, engine = stub_server_mod
    async with workflow_worker(temporal_env, WORKFLOWS, base_url=base_url, profile="execute") as (
        client,
        task_queue,
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await asyncio.wait_for(client.execute_workflow(
                DenialFailsFastWorkflow.run,
                args=[BELOW_RISK_FLOOR_DECISION_ID, "https://ops.example.com/hooks/yolo"],
                id=f"wf-{uuid.uuid4().hex}",
                task_queue=task_queue,
            ), timeout=150)

    # The structured denial survives the whole chain: activity -> workflow -> client.
    denial = denial_from_activity_error(exc_info.value)
    assert denial is not None
    assert denial.gate == "risk_floor"
    assert denial.code == "execution_blocked_risk_floor"
    # maximum_attempts=5 on the policy, but a denial is non-retryable: exactly one call.
    assert engine.execute_attempts[BELOW_RISK_FLOOR_DECISION_ID] == 1
