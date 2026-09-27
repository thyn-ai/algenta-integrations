"""Recipe 1 -- Durable governed decision workflow.

The canonical Temporal "hello world" -- a workflow orchestrating activities -- applied to the
one sequence every Algenta user needs: `log_decision` (persist the decision to decision
memory, get a `decision_id`) followed by `execute_decision` (dispatch it to a webhook under
policy gates). Each step is a durable, retried Temporal activity; the workflow's result *is*
the typed execution receipt, so "did the governed action actually land?" is answered by the
workflow history itself.

Run it (zero credentials -- starts a demo engine + a local time-skipping Temporal server):

    uv run python -m recipes.durable_decision_workflow

Against your own engine + Temporal server instead:

    ALGENTA_BASE_URL=http://localhost:8000/mcp TEMPORAL_ADDRESS=localhost:7233 \
        uv run python -m recipes.durable_decision_workflow
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
        GovernedDecisionInput,
        LogDecisionInput,
    )


@workflow.defn
class GovernedDecisionWorkflow:
    """Log one governed decision, then execute it; the workflow result is the receipt.

    The execute step's `RetryPolicy` names the three real denial codes as non-retryable
    (defense-in-depth -- `temporal_algenta` already raises denials `non_retryable=True`; see
    `recipes/retry_policy_structured_denials.py`): a policy denial is deterministic, so the
    only correct retry count for it is zero.
    """

    @workflow.run
    async def run(self, input: GovernedDecisionInput) -> ExecutionReceiptData:
        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(chosen_action=input.action, rationale=input.rationale),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return await workflow.execute_activity(
            AlgentaActivities.execute_decision,
            ExecuteDecisionInput(decision_id=logged["decision_id"], webhook_url=input.webhook_url),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=4, non_retryable_error_types=sorted(DENIAL_ERROR_TYPES)),
        )


async def main() -> None:
    from recipes._runner import recipe_worker

    async with recipe_worker([GovernedDecisionWorkflow], profile="execute") as (client, task_queue):
        receipt = await client.execute_workflow(
            GovernedDecisionWorkflow.run,
            GovernedDecisionInput(
                action="hold", webhook_url="https://ops.example.com/hooks/rebalance", rationale="demo run"
            ),
            id=f"governed-decision-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print("Governed decision executed durably.")
        print(f"  decision_id:       {receipt.decision_id}")
        print(f"  execution_status:  {receipt.execution_status} (response_code={receipt.response_code})")
        print(f"  executed_at:       {receipt.executed_at}")
        print(f"  policy_snapshot:   {receipt.policy_snapshot_id}")
        print(f"  safety_overridden: {receipt.safety_overridden}")


if __name__ == "__main__":
    asyncio.run(main())
