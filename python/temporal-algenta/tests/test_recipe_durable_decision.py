"""Recipe 1 tests: the durable decision workflow logs then executes, and its result is the
receipt -- with the engine state to prove the delivery really happened.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.durable_decision_workflow import GovernedDecisionWorkflow
from temporal_algenta.types import ExecutionReceiptData, GovernedDecisionInput

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_governed_decision_workflow_returns_the_execution_receipt(temporal_env, engine_state) -> None:
    base_url, engine = engine_state
    async with workflow_worker(
        temporal_env, [GovernedDecisionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        receipt = await asyncio.wait_for(client.execute_workflow(
            GovernedDecisionWorkflow.run,
            GovernedDecisionInput(action="hold", webhook_url="https://ops.example.com/hooks/hold"),
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=ExecutionReceiptData,
        ), timeout=150)

    assert receipt.decision_id == "decision-hold"
    assert receipt.is_delivered()
    assert receipt.response_code == 200
    # The engine really logged and delivered exactly this decision -- the workflow history and
    # the engine state agree.
    assert engine.delivered_decision_ids == {"decision-hold"}
    assert [d["decision_id"] for d in engine.logged_decisions] == ["decision-hold"]
