"""Recipe 2 tests: the human-approval workflow parks on a policy denial, exposes it via query,
and only an operator signal with an explicit override restarts execution.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.human_approval_workflow import (
    ApprovalGatedInput,
    ApprovalStatus,
    HumanApprovalExecutionWorkflow,
)
from temporal_algenta.types import ApprovalDecision, ExecutionReceiptData
from temporalio.client import WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError

from .helpers import workflow_worker


async def _wait_for_state(handle: WorkflowHandle, wanted: str, *, timeout: float = 30.0) -> ApprovalStatus:
    from temporalio.service import RPCError

    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            status = await handle.query(HumanApprovalExecutionWorkflow.status, result_type=ApprovalStatus)
        except RPCError:
            # Transient: the dev server rejects queries until the worker's poller has
            # registered on the task queue ("no poller seen ... recently") -- keep polling.
            status = None
        if status is not None and status.state == wanted:
            return status
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"workflow never reached state {wanted!r}; last status: {status}")
        await asyncio.sleep(0.1)


def _typed_result_handle(client, handle: WorkflowHandle) -> WorkflowHandle:
    # `start_workflow` handles don't carry a result_type; rebind one so the receipt comes back
    # as an ExecutionReceiptData, not a dict.
    return client.get_workflow_handle(handle.id, result_type=ExecutionReceiptData)


async def _start(client, task_queue: str, input: ApprovalGatedInput) -> WorkflowHandle:
    return await client.start_workflow(
        HumanApprovalExecutionWorkflow.run,
        input,
        id=f"wf-{uuid.uuid4().hex}",
        task_queue=task_queue,
    )


async def test_denial_parks_workflow_and_operator_approval_completes_it(temporal_env_realtime, stub_server) -> None:
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env_realtime, [HumanApprovalExecutionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        handle = await _start(
            client, task_queue, ApprovalGatedInput(action="low-confidence", webhook_url="http://ops/hook")
        )
        status = await _wait_for_state(handle, "awaiting_approval")
        assert status.denial_gate == "confidence"
        assert status.denial_message is not None
        assert status.override_hint is not None

        await handle.signal(
            HumanApprovalExecutionWorkflow.decide,
            ApprovalDecision(approved=True, operator="jane.doe@example.com", reason="reviewed", override_safety=True),
        )
        receipt = await asyncio.wait_for(_typed_result_handle(client, handle).result(), timeout=150)
        final = await handle.query(HumanApprovalExecutionWorkflow.status, result_type=ApprovalStatus)

    assert receipt.decision_id == "decision-low-confidence"
    assert receipt.is_delivered()
    # The override came from the operator's signal -- and the receipt proves it was needed.
    assert receipt.safety_overridden is True
    assert engine.delivered_decision_ids == {"decision-low-confidence"}
    assert final.state == "completed"
    assert final.decided_by == "jane.doe@example.com"


async def test_operator_rejection_fails_the_workflow_without_delivering(temporal_env_realtime, stub_server) -> None:
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env_realtime, [HumanApprovalExecutionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        handle = await _start(
            client, task_queue, ApprovalGatedInput(action="risky", webhook_url="http://ops/hook")
        )
        status = await _wait_for_state(handle, "awaiting_approval")
        assert status.denial_gate == "risk_floor"

        await handle.signal(
            HumanApprovalExecutionWorkflow.decide,
            ApprovalDecision(approved=False, operator="john.doe@example.com", reason="too risky"),
        )
        with pytest.raises(WorkflowFailureError) as exc_info:
            await asyncio.wait_for(handle.result(), timeout=150)

    cause = exc_info.value.__cause__
    assert isinstance(cause, ApplicationError)
    assert cause.type == "approval_rejected"
    assert "john.doe@example.com" in str(cause)
    assert engine.delivered_decision_ids == set()


async def test_approval_timeout_abandons_the_execution(temporal_env, stub_server) -> None:
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env, [HumanApprovalExecutionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        handle = await _start(
            client,
            task_queue,
            # One-second durable deadline; the time-skipping environment fast-forwards to it.
            # No pre-query of the parked state here: on a time-skipping server the query races
            # the skip and loses (see temporal_env_realtime's docstring).
            ApprovalGatedInput(action="risky", webhook_url="http://ops/hook", approval_timeout_seconds=1.0),
        )
        with pytest.raises(WorkflowFailureError) as exc_info:
            await asyncio.wait_for(handle.result(), timeout=150)
    cause = exc_info.value.__cause__
    assert isinstance(cause, ApplicationError)
    assert cause.type == "approval_timeout"
    assert engine.delivered_decision_ids == set()


async def test_clean_execution_never_parks(temporal_env_realtime, stub_server) -> None:
    base_url, _engine = stub_server
    async with workflow_worker(
        temporal_env_realtime, [HumanApprovalExecutionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        receipt = await client.execute_workflow(
            HumanApprovalExecutionWorkflow.run,
            ApprovalGatedInput(action="hold", webhook_url="http://ops/hook"),
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=ExecutionReceiptData,
        )
    assert receipt.is_delivered()
    assert not receipt.safety_overridden
