"""Recipe 6 -- Retry policy interacting with structured denials.

Every Temporal user tunes `RetryPolicy`; the governance question is *which failures deserve
retries at all*. This recipe runs the two halves side by side:

- **Transient failure -> retry.** `TransientTolerantQueryWorkflow` queries a dataset whose
  first two queries fail with a generic, non-denial tool error (an engine restarting mid-call
  -- the demo engine's `"flaky"` dataset). The activity maps it to a *retryable* typed
  `ApplicationError` (`algenta_tool_error`), and the policy's backoff rides it out: attempts
  1-2 fail, attempt 3 succeeds, the workflow completes normally.
- **Policy denial -> fail fast.** `DenialFailsFastWorkflow` executes a decision the engine
  blocks on the `risk_floor` gate. The denial arrives as a *non-retryable* `ApplicationError`
  whose `type` is the engine's own denial code; the workflow fails on the very first attempt
  and the structured denial is recoverable from the exception chain with
  `temporal_algenta.types.denial_from_activity_error`.

Both policies also *list* the three denial codes in `non_retryable_error_types` -- explicit,
self-documenting defense-in-depth on top of the `non_retryable=True` the package already
raises (either mechanism alone would stop the retries; listing both keeps the intent visible
in workflow code and robust against a caller that re-raises denials itself).

Run it: `uv run python -m recipes.retry_policy_structured_denials`
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import (
        DENIAL_ERROR_TYPES,
        ExecuteDecisionInput,
        ExecutionReceiptData,
    )

#: One shared policy shape for the recipe: bounded retries for transient failures, zero
#: retries for the three named policy gates.
GOVERNED_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=5,
    non_retryable_error_types=sorted(DENIAL_ERROR_TYPES),
)


@workflow.defn
class TransientTolerantQueryWorkflow:
    """Query a dataset behind a flaky engine; the retry policy absorbs the transient errors."""

    @workflow.run
    async def run(self, dataset: str) -> dict:
        return await workflow.execute_activity(
            AlgentaActivities.query_data,
            dataset,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=GOVERNED_RETRY_POLICY,
        )


@workflow.defn
class DenialFailsFastWorkflow:
    """Execute a decision the engine will deny; the denial must surface on attempt one."""

    @workflow.run
    async def run(self, decision_id: str, webhook_url: str) -> ExecutionReceiptData:
        return await workflow.execute_activity(
            AlgentaActivities.execute_decision,
            ExecuteDecisionInput(decision_id=decision_id, webhook_url=webhook_url),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=GOVERNED_RETRY_POLICY,
        )


async def main() -> None:
    from temporal_algenta.types import denial_from_activity_error
    from temporalio.client import WorkflowFailureError

    from recipes._runner import recipe_worker
    from recipes.demo_engine import BELOW_RISK_FLOOR_DECISION_ID, FLAKY_DATASET

    workflows = [TransientTolerantQueryWorkflow, DenialFailsFastWorkflow]
    async with recipe_worker(workflows, profile="execute") as (client, task_queue):
        # Transient half: two generic engine errors, then success -- retried transparently.
        result = await client.execute_workflow(
            TransientTolerantQueryWorkflow.run,
            FLAKY_DATASET,
            id=f"retry-transient-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print(f"Transient half: query succeeded after engine-side flakes -> {result}")

        # Denial half: the risk_floor gate blocks execution; the workflow fails immediately,
        # and the structured denial survives the whole exception chain to the client.
        try:
            await client.execute_workflow(
                DenialFailsFastWorkflow.run,
                args=[BELOW_RISK_FLOOR_DECISION_ID, "https://ops.example.com/hooks/yolo"],
                id=f"retry-denial-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
        except WorkflowFailureError as error:
            denial = denial_from_activity_error(error)
            print("Denial half: workflow failed fast on the first attempt (no wasted retries).")
            print(f"  gate: {denial.gate if denial else None}; code: {denial.code if denial else None}")
            print(f"  engine said: {denial.message if denial else None}")


if __name__ == "__main__":
    asyncio.run(main())
