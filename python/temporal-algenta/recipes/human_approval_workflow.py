"""Recipe 2 -- Human-in-the-loop approval workflow.

Temporal's most popular durability pattern after plain orchestration: pause a workflow until a
human signals it. Applied to Algenta governance, this is the honest answer to "what happens
when policy blocks my action?":

1. The workflow logs and tries to execute a decision.
2. The engine blocks it synchronously on a named policy gate (`confidence` / `risk_floor`) --
   surfacing as a typed, non-retryable `ApplicationError` carrying the structured denial.
3. Instead of failing, the workflow parks itself (`workflow.wait_condition`) and exposes the
   pending denial via `@workflow.query` -- durable for days if the approver is on vacation,
   with zero polling and zero held connections.
4. An operator reviews and signals `decide(ApprovalDecision(...))`. Only that human signal can
   set `override_safety=True` for the retry -- the exact operator/break-glass path the shared
   contract reserves those fields for ("outside the model-facing tool call entirely"). No
   model, planner, or default can turn it on.
5. The retried execution returns a receipt with `safety_overridden=True` -- the audit trail of
   who approved and why lives in the workflow history next to the receipt.

The engine intentionally exposes no approval round trip over MCP (see
`temporal_algenta.receipts`), so approval is built from Temporal's own primitives -- signals,
queries, and durable timers -- rather than a fictional "pending" receipt state.

Run it: `uv run python -m recipes.human_approval_workflow`
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import (
        ApprovalDecision,
        DenialDetails,
        ExecuteDecisionInput,
        ExecutionReceiptData,
        LogDecisionInput,
        denial_from_activity_error,
    )


@dataclass
class ApprovalGatedInput:
    """Input for `HumanApprovalExecutionWorkflow`: the governed decision to attempt, plus the
    durable approval deadline."""

    action: str
    webhook_url: str
    rationale: str | None = None
    approval_timeout_seconds: float = 3600.0
    """How long the workflow waits for an operator signal before abandoning the execution. A
    durable timer -- the workflow holds no resources while waiting."""


@dataclass
class ApprovalStatus:
    """The workflow's operator-facing status, exposed via `@workflow.query`."""

    state: str
    """One of `"executing"`, `"awaiting_approval"`, `"approved"`, `"rejected"`, `"completed"`."""
    denial_gate: str | None = None
    denial_message: str | None = None
    override_hint: str | None = None
    decided_by: str | None = None


@workflow.defn
class HumanApprovalExecutionWorkflow:
    """Execute a governed decision; on a policy denial, wait for a human's signal.

    The `decide` signal is the *only* way `override_safety` ever reaches `execute_decision`
    here -- and it arrives from an identified operator with a recorded reason, which is what
    makes this an approval workflow rather than a silent bypass.
    """

    def __init__(self) -> None:
        self._approval: ApprovalDecision | None = None
        self._denial: DenialDetails | None = None
        self._state = "executing"

    @workflow.run
    async def run(self, input: ApprovalGatedInput) -> ExecutionReceiptData:
        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(chosen_action=input.action, rationale=input.rationale),
            start_to_close_timeout=timedelta(seconds=30),
        )
        decision_id = logged["decision_id"]

        try:
            receipt = await self._execute(decision_id, input.webhook_url)
            self._state = "completed"
            return receipt
        except ActivityError as error:
            denial = denial_from_activity_error(error)
            if denial is None or denial.gate == "idempotency":
                # Not a policy gate a human can meaningfully override (or not a denial at
                # all) -- fail the workflow rather than parking it.
                raise
            self._denial = denial
            self._state = "awaiting_approval"

        try:
            await workflow.wait_condition(
                lambda: self._approval is not None,
                timeout=timedelta(seconds=input.approval_timeout_seconds),
            )
        except TimeoutError:
            self._state = "rejected"
            raise ApplicationError(
                f"No operator decision arrived within {input.approval_timeout_seconds}s; execution of "
                f"{decision_id!r} (blocked on the {self._denial.gate!r} gate) was abandoned.",
                type="approval_timeout",
                non_retryable=True,
            ) from None

        assert self._approval is not None  # wait_condition returned
        if not self._approval.approved:
            self._state = "rejected"
            raise ApplicationError(
                f"Operator {self._approval.operator!r} rejected execution of {decision_id!r}: "
                f"{self._approval.reason or 'no reason given'}",
                type="approval_rejected",
                non_retryable=True,
            )

        self._state = "approved"
        receipt = await self._execute(
            decision_id, input.webhook_url, override_safety=self._approval.override_safety
        )
        self._state = "completed"
        return receipt

    async def _execute(self, decision_id: str, webhook_url: str, *, override_safety: bool = False) -> ExecutionReceiptData:
        return await workflow.execute_activity(
            AlgentaActivities.execute_decision,
            ExecuteDecisionInput(
                decision_id=decision_id, webhook_url=webhook_url, override_safety=override_safety
            ),
            start_to_close_timeout=timedelta(seconds=60),
        )

    @workflow.signal
    def decide(self, decision: ApprovalDecision) -> None:
        """Record the operator's verdict. The first decision wins; later signals are ignored so
        a completed approval can't be rewritten."""
        if self._approval is None:
            self._approval = decision

    @workflow.query
    def status(self) -> ApprovalStatus:
        return ApprovalStatus(
            state=self._state,
            denial_gate=self._denial.gate if self._denial else None,
            denial_message=self._denial.message if self._denial else None,
            override_hint=self._denial.override_hint if self._denial else None,
            decided_by=self._approval.operator if self._approval else None,
        )


async def main() -> None:
    from recipes._runner import recipe_worker

    async with recipe_worker([HumanApprovalExecutionWorkflow], profile="execute") as (client, task_queue):
        handle = await client.start_workflow(
            HumanApprovalExecutionWorkflow.run,
            ApprovalGatedInput(action="low-confidence", webhook_url="https://ops.example.com/hooks/rebalance"),
            id=f"approval-gated-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        # The demo engine blocks "decision-low-confidence" on the confidence gate. Give the
        # workflow a moment to reach its parked state, then read the pending denial...
        await asyncio.sleep(2)
        status = await handle.query(HumanApprovalExecutionWorkflow.status)
        print(f"Workflow parked: state={status.state}, gate={status.denial_gate}")
        print(f"  engine says:   {status.denial_message}")
        print(f"  override hint: {status.override_hint}")
        # ...and an operator approves, explicitly authorizing the break-glass override.
        await handle.signal(
            HumanApprovalExecutionWorkflow.decide,
            ApprovalDecision(approved=True, operator="jane.doe@example.com", reason="manual review OK", override_safety=True),
        )
        receipt = await handle.result()
        print(f"Approved and executed: {receipt.decision_id} -> {receipt.execution_status}")
        print(f"  safety_overridden: {receipt.safety_overridden} (set by the operator's signal)")


if __name__ == "__main__":
    asyncio.run(main())
