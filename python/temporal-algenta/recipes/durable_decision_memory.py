"""Recipe 7 -- Durable decision memory.

A long-running "entity" workflow -- another signature Temporal pattern: the workflow *is* the
journal. Operators record decisions into it via signals; each one is persisted to Algenta's
decision memory through a governed `log_decision` activity (so the durable record lives
engine-side with a receipt-shaped audit trail), and mirrored into the workflow's own
deterministic state, queryable at any time via `@workflow.query` -- no database, no polling,
and the journal survives worker restarts by construction.

Signals are processed one at a time in arrival order, so the journal's ordering is the order
of record -- exactly what an audit journal needs -- and `entries()` reads never block the
writers.

Run it: `uv run python -m recipes.durable_decision_memory`
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import LogDecisionInput


@dataclass
class RecordDecisionCommand:
    """One journal write, delivered via the `record` signal."""

    action: str
    rationale: str | None = None


@dataclass
class JournalEntry:
    """One journaled decision: the engine's decision-memory record, mirrored in workflow state."""

    seq: int
    decision_id: str
    action: str
    rationale: str | None
    engine_created_at: str


@workflow.defn
class DecisionJournalWorkflow:
    """A durable decision journal: writes via signal, reads via query, close via signal."""

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._closed = False

    @workflow.run
    async def run(self) -> dict:
        """Stays open (and durable) until the `close` signal arrives, then returns a summary."""
        await workflow.wait_condition(lambda: self._closed)
        return {
            "decision_count": len(self._entries),
            "decision_ids": [entry.decision_id for entry in self._entries],
        }

    @workflow.signal
    async def record(self, command: RecordDecisionCommand) -> None:
        """Persist one decision to decision memory, then mirror it into the journal."""
        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(chosen_action=command.action, rationale=command.rationale),
            start_to_close_timeout=timedelta(seconds=30),
        )
        self._entries.append(
            JournalEntry(
                seq=len(self._entries) + 1,
                decision_id=logged["decision_id"],
                action=command.action,
                rationale=command.rationale,
                engine_created_at=logged["created_at"],
            )
        )

    @workflow.signal
    def close(self) -> None:
        self._closed = True

    @workflow.query
    def entries(self) -> list[JournalEntry]:
        return list(self._entries)


async def main() -> None:
    from recipes._runner import recipe_worker

    async with recipe_worker([DecisionJournalWorkflow], profile="govern") as (client, task_queue):
        handle = await client.start_workflow(
            DecisionJournalWorkflow.run,
            id=f"decision-journal-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        await handle.signal(
            DecisionJournalWorkflow.record,
            RecordDecisionCommand(action="hold", rationale="volatility too high to rebalance"),
        )
        # Signals from separate client calls are processed in arrival order; spacing the second
        # one keeps the printed journal ordered the way this demo narrates it.
        await asyncio.sleep(1)
        await handle.signal(
            DecisionJournalWorkflow.record,
            RecordDecisionCommand(action="rebalance", rationale="drift above tolerance"),
        )
        # Queries never block in-flight signal work; read the journal mid-flight.
        await asyncio.sleep(2)
        entries = await handle.query(DecisionJournalWorkflow.entries)
        for entry in entries:
            print(f"  #{entry.seq} {entry.decision_id}: {entry.action} ({entry.rationale})")
        await handle.signal(DecisionJournalWorkflow.close)
        summary = await handle.result()
        print(f"Journal closed with {summary['decision_count']} decisions: {summary['decision_ids']}")


if __name__ == "__main__":
    asyncio.run(main())
