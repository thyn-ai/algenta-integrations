"""Recipe 9 -- Durable agent workflow with a governed tool surface.

"Durable AI agent" is Temporal's hottest current use case: an agent loop whose every step is a
durable activity, so a crashed agent resumes exactly where it stopped. This recipe is that
loop with Algenta as the tool layer -- and the governance point is that the agent's *tool
list* is the shared contract's profile-filtered surface, not "whatever the engine has":

- The workflow is configured with a tool profile (`observe` / `govern` / `execute` / `full`).
- The planner's advertised tool set is resolved from the embedded contract (`TOOL_PROFILES`)
  -- under `observe`, `execute_decision` isn't just blocked, it isn't *advertised*; a plan
  step naming it is refused by the workflow itself, before any activity runs. (The activities
  enforce the same profile again at call time -- defense in depth.)
- The planner here is a fixed, deterministic script -- `recommend -> plan_decision ->
  log_decision -> execute_decision` -- so the recipe runs with no LLM credentials and is fully
  replayable. The seam for a real model is `AGENT_PLAN`: swap the constant for your LLM's
  tool-choice output (produced by an activity, then iterated exactly like below) and the
  governance story is unchanged, because filtering happens against the advertised set, not
  against who proposed the call.

`main()` runs the loop twice: under `execute` the full plan completes with a receipt; under
`govern` the agent may plan and record but the execution step is refused and the run reports
it -- the audit-worthy outcome for an agent that was never supposed to act.

Run it: `uv run python -m recipes.agent_workflow_governed_tools`
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.contract import FULL_PROFILE_SENTINEL, TOOL_PROFILES, ToolProfile
    from temporal_algenta.types import ExecuteDecisionInput, ExecutionReceiptData, LogDecisionInput

#: The deterministic stand-in for an LLM planner's tool-choice sequence (see the module
#: docstring for the seam this creates for a real model).
AGENT_PLAN: Final[tuple[str, ...]] = ("recommend", "plan_decision", "log_decision", "execute_decision")


@dataclass
class AgentStepResult:
    """One planned tool call's outcome."""

    tool: str
    status: str
    """`"ok"` (the activity ran) or `"refused_by_profile"` (the tool wasn't advertised under
    the workflow's profile, so nothing ran -- by design)."""
    summary: str


@dataclass
class AgentRunReport:
    """The full run: every step's fate, plus the receipt id when execution happened."""

    profile: str
    steps: list[AgentStepResult] = field(default_factory=list)
    executed_decision_id: str | None = None
    receipt_status: str | None = None


@workflow.defn
class GovernedAgentWorkflow:
    """Iterate a tool plan under a profile-filtered tool surface, one durable step at a time.

    `profile` is an operator-chosen workflow input, deliberately independent of the worker's
    own `AlgentaActivities(profile=...)`: the workflow refuses non-advertised tools *before*
    spending an activity, and the activity layer still enforces the worker's profile at call
    time. Safest real deployments run both at the same level; making it an input here is what
    lets one recipe demonstrate the boundary.
    """

    @workflow.run
    async def run(self, scenario: str, webhook_url: str, profile: ToolProfile) -> AgentRunReport:
        allowed = TOOL_PROFILES[profile]
        report = AgentRunReport(profile=profile)
        decision_id: str | None = None

        for tool in AGENT_PLAN:
            if allowed != FULL_PROFILE_SENTINEL and tool not in allowed:
                report.steps.append(
                    AgentStepResult(
                        tool=tool,
                        status="refused_by_profile",
                        summary=f"{tool!r} is not advertised under the {profile!r} profile; skipped before any call",
                    )
                )
                continue
            result = await self._call_tool(tool, scenario, webhook_url, decision_id)
            if tool == "log_decision":
                assert isinstance(result, dict)
                decision_id = result["decision_id"]
            elif tool == "execute_decision":
                assert isinstance(result, ExecutionReceiptData)
                report.executed_decision_id = result.decision_id
                report.receipt_status = result.execution_status
            report.steps.append(AgentStepResult(tool=tool, status="ok", summary=_summarize(tool, result)))

        return report

    async def _call_tool(self, tool: str, scenario: str, webhook_url: str, decision_id: str | None) -> object:
        if tool == "recommend":
            return await workflow.execute_activity(
                AlgentaActivities.recommend, scenario, start_to_close_timeout=timedelta(seconds=30)
            )
        if tool == "plan_decision":
            return await workflow.execute_activity(
                AlgentaActivities.plan_decision, scenario, start_to_close_timeout=timedelta(seconds=30)
            )
        if tool == "log_decision":
            return await workflow.execute_activity(
                AlgentaActivities.log_decision,
                LogDecisionInput(chosen_action=f"agent-{scenario}", rationale="planned by GovernedAgentWorkflow"),
                start_to_close_timeout=timedelta(seconds=30),
            )
        if tool == "execute_decision":
            if decision_id is None:
                raise ValueError("plan ordered execute_decision before log_decision; no decision_id available")
            return await workflow.execute_activity(
                AlgentaActivities.execute_decision,
                ExecuteDecisionInput(decision_id=decision_id, webhook_url=webhook_url),
                start_to_close_timeout=timedelta(seconds=60),
            )
        raise ValueError(f"unknown planned tool {tool!r}")


def _summarize(tool: str, result: object) -> str:
    if isinstance(result, ExecutionReceiptData):
        return f"execution_status={result.execution_status} response_code={result.response_code}"
    if isinstance(result, dict):
        return ", ".join(f"{key}={result[key]}" for key in sorted(result)[:3])
    return repr(result)


async def main() -> None:
    from recipes._runner import recipe_worker

    async with recipe_worker([GovernedAgentWorkflow], profile="execute") as (client, task_queue):
        full = await client.execute_workflow(
            GovernedAgentWorkflow.run,
            args=["spx-rebalance", "https://ops.example.com/hooks/rebalance", "execute"],
            id=f"agent-execute-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print(f"[profile=execute] steps: {[(step.tool, step.status) for step in full.steps]}")
        print(f"  executed: {full.executed_decision_id} -> {full.receipt_status}")

        # The same worker (same activity profile) running the same plan, but the workflow is
        # told its advertised surface is `govern`: the agent may plan and record, but the
        # execution step is refused before any activity runs.
        governed = await client.execute_workflow(
            GovernedAgentWorkflow.run,
            args=["spx-rebalance", "https://ops.example.com/hooks/rebalance", "govern"],
            id=f"agent-govern-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        print(f"[profile=govern] steps: {[(step.tool, step.status) for step in governed.steps]}")
        print(f"  executed: {governed.executed_decision_id} (nothing executed -- by design)")


if __name__ == "__main__":
    asyncio.run(main())
