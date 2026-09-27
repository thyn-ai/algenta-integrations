"""Recipe 5 -- Saga with policy gates.

The saga pattern -- Temporal's flagship answer to distributed transactions: run a sequence of
real-world steps, and if any step fails, unwind the completed ones with compensations in
reverse order. Here every forward step is a governed `execute_decision`, and the failure that
triggers the unwind is not a crash but a *policy denial*: the engine refuses step N on a named
gate, synchronously, and the saga compensates steps 1..N-1.

What a compensation is, honestly: the engine cannot un-deliver a webhook (the side effect
already happened). What it can do -- and what this recipe does -- is record a compensating
decision in decision memory (`log_decision` with a note naming the decision it reverses), so
the unwind is itself governed, durable, and auditable. A production saga would call its own
domain reversal endpoint in the same place (an extra activity next to the `log_decision`
call); the governance shape is identical either way.

The workflow never throws for an expected denial: it returns a `SagaReport` naming the gate,
the executed steps, and the compensations -- a deterministic, queryable outcome rather than a
stack trace.

Run it: `uv run python -m recipes.saga_with_policy_gates`
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import (
        ExecuteDecisionInput,
        LogDecisionInput,
        denial_from_activity_error,
    )


@dataclass
class SagaStep:
    """One governed forward step of the saga."""

    action: str
    webhook_url: str


@dataclass
class SagaReport:
    """The saga's deterministic outcome."""

    status: str
    """`"completed"` (every step delivered) or `"compensated"` (a policy denial stopped the
    saga and every delivered step was compensated in reverse order)."""
    executed_decision_ids: list[str] = field(default_factory=list)
    compensated_decision_ids: list[str] = field(default_factory=list)
    denial_gate: str | None = None
    denial_message: str | None = None


@workflow.defn
class PolicyGatedSagaWorkflow:
    """Execute a sequence of governed decisions; on a policy denial, compensate in reverse."""

    @workflow.run
    async def run(self, steps: list[SagaStep]) -> SagaReport:
        executed: list[str] = []
        for step in steps:
            logged = await workflow.execute_activity(
                AlgentaActivities.log_decision,
                LogDecisionInput(chosen_action=step.action),
                start_to_close_timeout=timedelta(seconds=30),
            )
            decision_id = logged["decision_id"]
            try:
                await workflow.execute_activity(
                    AlgentaActivities.execute_decision,
                    ExecuteDecisionInput(decision_id=decision_id, webhook_url=step.webhook_url),
                    start_to_close_timeout=timedelta(seconds=60),
                )
            except ActivityError as error:
                denial = denial_from_activity_error(error)
                if denial is None:
                    # Not a policy denial -- a real failure, not a governance outcome; let the
                    # workflow fail loudly rather than compensating around an unknown state.
                    raise
                compensated = await self._compensate(reversed(executed), blocked_decision_id=decision_id)
                return SagaReport(
                    status="compensated",
                    executed_decision_ids=executed,
                    compensated_decision_ids=compensated,
                    denial_gate=denial.gate,
                    denial_message=denial.message,
                )
            executed.append(decision_id)
        return SagaReport(status="completed", executed_decision_ids=executed)

    async def _compensate(self, decision_ids: Iterable[str], *, blocked_decision_id: str) -> list[str]:
        """Record one compensating decision per delivered step, in reverse order.

        The compensation references both the decision it reverses and the denial that triggered
        the unwind, so the audit chain (forward receipt -> denial -> compensation) is fully
        traceable in decision memory.
        """
        compensated: list[str] = []
        for executed_id in decision_ids:
            record = await workflow.execute_activity(
                AlgentaActivities.log_decision,
                LogDecisionInput(
                    chosen_action=f"compensate-{executed_id.removeprefix('decision-')}",
                    note=f"compensates {executed_id}; saga stopped when {blocked_decision_id} was denied",
                ),
                start_to_close_timeout=timedelta(seconds=30),
            )
            compensated.append(record["decision_id"])
        return compensated


async def main() -> None:
    from recipes._runner import recipe_worker

    steps = [
        SagaStep(action="hold", webhook_url="https://ops.example.com/hooks/hold"),
        # The demo engine blocks "decision-risky" on the risk_floor gate -- the step that
        # stops this saga and triggers the compensation of "decision-hold".
        SagaStep(action="risky", webhook_url="https://ops.example.com/hooks/yolo"),
        SagaStep(action="settle", webhook_url="https://ops.example.com/hooks/settle"),
    ]
    async with recipe_worker([PolicyGatedSagaWorkflow], profile="execute") as (client, task_queue):
        report = await client.execute_workflow(
            PolicyGatedSagaWorkflow.run,
            steps,
            id=f"saga-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print(f"Saga {report.status}: gate={report.denial_gate}")
        print(f"  executed:    {report.executed_decision_ids}")
        print(f"  compensated: {report.compensated_decision_ids}")
        print(f"  engine said: {report.denial_message}")


if __name__ == "__main__":
    asyncio.run(main())
