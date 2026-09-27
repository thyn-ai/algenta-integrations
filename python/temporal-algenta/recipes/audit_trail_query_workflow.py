"""Recipe 10 -- Audit trail query workflow.

Two audit streams, one report. As the workflow makes governed calls (`log_decision`,
`execute_decision`) it records a deterministic audit event per step in its own state --
sequence-numbered by the workflow (no clocks, so the trail is identical under replay) and
readable live via `@workflow.query`. When the run finishes, it pulls the *engine-side* audit
dataset (`query_data("audit_log")` -- decision-memory writes and deliveries as the engine saw
them) and returns both streams merged: what the workflow intended, next to what the engine
recorded, linked by `decision_id`.

This is the "audit-trailed execution" pattern compliance teams actually ask for, built from
two Temporal primitives (workflow state + queries) and Algenta's governed decision memory --
no side database, and the trail survives worker restarts with the workflow history.

Run it: `uv run python -m recipes.audit_trail_query_workflow`
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import (
        AuditEvent,
        ExecuteDecisionInput,
        ExecutionReceiptData,
        GovernedDecisionInput,
        LogDecisionInput,
    )

#: The engine-side audit dataset this recipe reconciles against (see `recipes/demo_engine.py`;
#: a real engine exposes its audit/decision-memory datasets through the same `query_data`
#: governed-query path).
AUDIT_DATASET = "audit_log"


@dataclass
class AuditReport:
    """The merged outcome: the workflow-side trail plus the engine-side audit rows."""

    trail: list[AuditEvent] = field(default_factory=list)
    engine_audit_rows: list[dict[str, Any]] = field(default_factory=list)
    receipt_status: str | None = None
    decision_id: str | None = None


@workflow.defn
class AuditedDecisionWorkflow:
    """Run a governed decision while building a live, queryable audit trail."""

    def __init__(self) -> None:
        self._trail: list[AuditEvent] = []

    def _record(self, kind: str, detail: str, decision_id: str | None = None) -> None:
        self._trail.append(AuditEvent(seq=len(self._trail) + 1, kind=kind, detail=detail, decision_id=decision_id))

    @workflow.run
    async def run(self, input: GovernedDecisionInput) -> AuditReport:
        self._record("workflow_started", f"action={input.action} webhook_url={input.webhook_url}")

        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(chosen_action=input.action, rationale=input.rationale),
            start_to_close_timeout=timedelta(seconds=30),
        )
        decision_id = logged["decision_id"]
        self._record("decision_logged", f"decision_id={decision_id}", decision_id)

        receipt: ExecutionReceiptData = await workflow.execute_activity(
            AlgentaActivities.execute_decision,
            ExecuteDecisionInput(decision_id=decision_id, webhook_url=input.webhook_url),
            start_to_close_timeout=timedelta(seconds=60),
        )
        self._record(
            "decision_executed",
            f"execution_status={receipt.execution_status} response_code={receipt.response_code}",
            decision_id,
        )

        audit_rows = await workflow.execute_activity(
            AlgentaActivities.query_data,
            AUDIT_DATASET,
            start_to_close_timeout=timedelta(seconds=30),
        )
        rows = audit_rows["rows"]
        self._record("engine_audit_fetched", f"rows={len(rows)}", decision_id)

        return AuditReport(
            trail=list(self._trail),
            engine_audit_rows=rows,
            receipt_status=receipt.execution_status,
            decision_id=decision_id,
        )

    @workflow.query
    def audit_trail(self) -> list[AuditEvent]:
        """The workflow-side trail so far -- queryable live, mid-run, without side effects."""
        return list(self._trail)


async def main() -> None:
    from recipes._runner import recipe_worker

    async with recipe_worker([AuditedDecisionWorkflow], profile="execute") as (client, task_queue):
        handle = await client.start_workflow(
            AuditedDecisionWorkflow.run,
            GovernedDecisionInput(action="hold", webhook_url="https://ops.example.com/hooks/rebalance"),
            id=f"audited-decision-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        report = await handle.result()
        # Queries also work on the completed workflow -- the trail is part of its history.
        trail = await handle.query(AuditedDecisionWorkflow.audit_trail)
        print("Workflow-side audit trail (live-queryable):")
        for event in trail:
            print(f"  #{event.seq} {event.kind}: {event.detail}")
        print("Engine-side audit rows (decision memory):")
        for row in report.engine_audit_rows:
            print(f"  {row}")
        print(f"receipt: {report.decision_id} -> {report.receipt_status}")


if __name__ == "__main__":
    asyncio.run(main())
