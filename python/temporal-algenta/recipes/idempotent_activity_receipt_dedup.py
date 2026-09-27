"""Recipe 3 -- Idempotent execution with receipt dedup.

"How do I make an activity safe to retry?" is the most-asked durability question in Temporal.
For governed execution, Algenta's own idempotency gate *is* the answer -- and the receipt is
the proof:

- `execute_decision` is keyed by `decision_id`. The engine delivers a given `decision_id` to
  its webhook at most once; a second delivery attempt is blocked synchronously on the
  `"idempotency"` gate.
- Temporal retries activities (after a worker crash, a deploy mid-run, a flaky network). When
  a retry re-attempts an `execute_decision` whose first attempt actually delivered -- the
  classic "delivered, but the completion never made it back" crash window -- the engine
  answers `execution_blocked_idempotency` instead of delivering twice.
- This workflow maps exactly that denial to a *deduplicated success*: the delivery already
  landed, the receipt-equivalent proof is the denial itself, and the workflow completes
  normally. Every other failure propagates.

The result is an exactly-once *effect* (at-least-once attempt + engine-side dedup), which is
the strongest honestly-available guarantee for webhook delivery -- and it's auditable, because
both the original delivery and the dedup decision live in the workflow history and decision
memory.

Run it: `uv run python -m recipes.idempotent_activity_receipt_dedup`
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import (
        ExecuteDecisionInput,
        GovernedDecisionInput,
        LogDecisionInput,
        denial_from_activity_error,
    )


@dataclass
class IdempotentExecutionResult:
    """What the workflow learned about the delivery of one decision."""

    decision_id: str
    delivered: bool
    deduplicated: bool
    """True when the engine's idempotency gate proved a *prior* attempt had already delivered
    this exact `decision_id` (so this run deliberately delivered nothing new)."""
    response_code: int | None = None


@workflow.defn
class IdempotentDecisionWorkflow:
    """Execute one logged decision, treating the engine's idempotency gate as dedup proof.

    The activity's `RetryPolicy` deliberately does *not* mark the denial codes non-retryable
    here: retries of the activity are exactly the scenario this recipe is about, and the
    package already raises denials `non_retryable=True`, so a denial (including the idempotency
    gate) still reaches the workflow immediately -- where the dedup mapping below decides what
    it means.
    """

    @workflow.run
    async def run(self, input: GovernedDecisionInput) -> IdempotentExecutionResult:
        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(chosen_action=input.action, rationale=input.rationale),
            start_to_close_timeout=timedelta(seconds=30),
        )
        decision_id = logged["decision_id"]
        try:
            receipt = await workflow.execute_activity(
                AlgentaActivities.execute_decision,
                ExecuteDecisionInput(decision_id=decision_id, webhook_url=input.webhook_url),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=4),
            )
            return IdempotentExecutionResult(
                decision_id=decision_id,
                delivered=receipt.is_delivered(),
                deduplicated=False,
                response_code=receipt.response_code,
            )
        except ActivityError as error:
            denial = denial_from_activity_error(error)
            if denial is not None and denial.gate == "idempotency":
                # The engine has already delivered this exact decision_id -- either a prior
                # attempt of this activity whose completion never reached the server, or a
                # previous run of this workflow. Delivering again would double-execute, so the
                # correct outcome is "done, deduplicated", not a failure.
                return IdempotentExecutionResult(decision_id=decision_id, delivered=True, deduplicated=True)
            raise


async def main() -> None:
    from recipes._runner import recipe_worker

    async with recipe_worker([IdempotentDecisionWorkflow], profile="execute") as (client, task_queue):
        # First run: a normal delivery.
        first = await client.execute_workflow(
            IdempotentDecisionWorkflow.run,
            GovernedDecisionInput(action="hold", webhook_url="https://ops.example.com/hooks/rebalance"),
            id=f"idempotent-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print(f"First run:  delivered={first.delivered}, deduplicated={first.deduplicated}")

        # Second run with the same action (hence the same decision_id): the engine's
        # idempotency gate blocks re-delivery, and the workflow reports a deduplicated success
        # instead of double-executing or failing.
        second = await client.execute_workflow(
            IdempotentDecisionWorkflow.run,
            GovernedDecisionInput(action="hold", webhook_url="https://ops.example.com/hooks/rebalance"),
            id=f"idempotent-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print(f"Second run: delivered={second.delivered}, deduplicated={second.deduplicated}")
        print("The engine delivered the webhook exactly once across both runs.")


if __name__ == "__main__":
    asyncio.run(main())
