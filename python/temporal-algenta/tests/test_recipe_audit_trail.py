"""Recipe 10 tests: the audited decision workflow builds a live-queryable trail and reconciles
it with the engine-side audit dataset.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.audit_trail_query_workflow import AuditedDecisionWorkflow, AuditReport
from temporal_algenta.types import AuditEvent, GovernedDecisionInput

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_audited_workflow_returns_both_audit_streams(temporal_env, stub_server_mod) -> None:
    base_url, engine = stub_server_mod
    async with workflow_worker(
        temporal_env, [AuditedDecisionWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        handle = await client.start_workflow(
            AuditedDecisionWorkflow.run,
            GovernedDecisionInput(action="hold", webhook_url="https://ops.example.com/hooks/hold"),
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
        )
        report = await asyncio.wait_for(client.get_workflow_handle(handle.id, result_type=AuditReport).result(), timeout=150)
        # The trail is queryable even on the completed workflow.
        trail = await handle.query(AuditedDecisionWorkflow.audit_trail, result_type=list[AuditEvent])

    assert report.decision_id == "decision-hold"
    assert report.receipt_status == "delivered"

    # Workflow-side trail: deterministic, sequence-numbered, one event per governed step.
    assert [event.seq for event in report.trail] == [1, 2, 3, 4]
    assert [event.kind for event in report.trail] == [
        "workflow_started",
        "decision_logged",
        "decision_executed",
        "engine_audit_fetched",
    ]
    assert report.trail[1].decision_id == "decision-hold"
    assert trail == report.trail

    # Engine-side audit rows: what decision memory itself recorded, linked by decision_id.
    events_by_id = {row["event"]: row["decision_id"] for row in report.engine_audit_rows}
    assert events_by_id == {
        "decision_logged": "decision-hold",
        "decision_delivered": "decision-hold",
    }
    # And it agrees with the stub engine's own state.
    assert engine.delivered_decision_ids == {"decision-hold"}
