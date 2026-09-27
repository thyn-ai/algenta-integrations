"""Recipe 7 tests: the decision journal workflow records decisions via signals, serves them via
query, and closes with a summary -- all durable.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.durable_decision_memory import (
    DecisionJournalWorkflow,
    JournalEntry,
    RecordDecisionCommand,
)

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_journal_records_queries_and_closes(temporal_env, engine_state) -> None:
    base_url, engine = engine_state
    async with workflow_worker(
        temporal_env, [DecisionJournalWorkflow], base_url=base_url, profile="govern"
    ) as (client, task_queue):
        handle = await client.start_workflow(
            DecisionJournalWorkflow.run,
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
        )
        # Signals from separate client calls are processed in *arrival* order, and two
        # back-to-back RPCs may arrive in either order -- so the second signal is sent only
        # after the journal visibly holds the first (a query barrier), making the expected
        # ordering deterministic rather than racy.
        await handle.signal(
            DecisionJournalWorkflow.record,
            RecordDecisionCommand(action="hold", rationale="volatility too high"),
        )
        await _wait_for_entry_count(handle, 1)
        await handle.signal(
            DecisionJournalWorkflow.record,
            RecordDecisionCommand(action="rebalance", rationale="drift above tolerance"),
        )
        entries = await _wait_for_entry_count(handle, 2)

        assert [entry.seq for entry in entries] == [1, 2]
        assert entries[0].decision_id == "decision-hold"
        assert entries[0].engine_created_at == "2026-09-01T00:00:00Z"
        assert entries[1].decision_id == "decision-rebalance"
        assert entries[1].rationale == "drift above tolerance"

        await handle.signal(DecisionJournalWorkflow.close)
        summary = await asyncio.wait_for(handle.result(), timeout=150)

    assert summary == {
        "decision_count": 2,
        "decision_ids": ["decision-hold", "decision-rebalance"],
    }
    # Every journaled decision was really persisted to decision memory engine-side (order is
    # not asserted engine-side for the same arrival-order reason).
    assert {d["decision_id"] for d in engine.logged_decisions} == {
        "decision-hold",
        "decision-rebalance",
    }


async def _wait_for_entry_count(handle, wanted: int, *, timeout: float = 30.0) -> list[JournalEntry]:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        entries = await handle.query(DecisionJournalWorkflow.entries, result_type=list[JournalEntry])
        if len(entries) >= wanted:
            return entries
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"journal never reached {wanted} entries: {entries}")
        await asyncio.sleep(0.1)
