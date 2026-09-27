"""Recipe 4 -- Scheduled governed simulation (cron).

Temporal Schedules are the modern replacement for cron workflows -- and "run the risk
simulation every night and keep the result in decision memory" is the bread-and-butter
scheduled job for a decision platform. The schedule fires a `ScheduledSimulationWorkflow`
run; each run simulates the scenario and logs the outcome to decision memory via governed
activities, so every nightly evaluation is queryable later with its `decision_id`.

The recipe's `main()` creates a real `Schedule` with a cron expression (`"0 2 * * *"` -- every
night at 02:00), triggers it once immediately so the demo produces output, prints what the
schedule did, and cleans up. The workflow itself is ordinary -- the scheduling lives entirely
in Temporal's `Schedule` primitive, not in the workflow code.

Run it: `uv run python -m recipes.scheduled_governed_simulation`
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
class ScheduledSimulationResult:
    """One scheduled run's outcome: the simulation value and the decision-memory record id."""

    scenario: str
    expected_value: float
    confidence: float
    decision_id: str


@workflow.defn
class ScheduledSimulationWorkflow:
    """Simulate one scenario and record the outcome in decision memory.

    Deliberately profile-`govern` compatible: nothing here executes a real-world action, so the
    scheduled worker never needs the execute tier at all.
    """

    @workflow.run
    async def run(self, scenario: str) -> ScheduledSimulationResult:
        simulation = await workflow.execute_activity(
            AlgentaActivities.simulate,
            scenario,
            start_to_close_timeout=timedelta(seconds=60),
        )
        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(
                chosen_action=f"nightly-{scenario}",
                note=f"scheduled simulation: expected_value={simulation['expected_value']}",
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return ScheduledSimulationResult(
            scenario=scenario,
            expected_value=float(simulation["expected_value"]),
            confidence=float(simulation["confidence"]),
            decision_id=logged["decision_id"],
        )


async def main() -> None:
    from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleSpec

    from recipes._runner import recipe_worker

    async with recipe_worker([ScheduledSimulationWorkflow], profile="govern") as (client, task_queue):
        workflow_id = f"nightly-risk-sim-{uuid.uuid4().hex[:8]}"
        handle = await client.create_schedule(
            f"sched-{workflow_id}",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ScheduledSimulationWorkflow.run,
                    "eurusd-overnight",
                    id=workflow_id,
                    task_queue=task_queue,
                ),
                spec=ScheduleSpec(cron_expressions=["0 2 * * *"]),
            ),
            # Fire one run right now so the demo doesn't have to wait until 02:00.
            trigger_immediately=True,
        )
        try:
            # trigger_immediately fires the action asynchronously; the action's workflow id is
            # the configured one plus a timestamp suffix, so the run's real id comes from the
            # schedule's recent_actions.
            action_result = None
            deadline = asyncio.get_event_loop().time() + 30.0
            while action_result is None:
                description = await handle.describe()
                if description.info.num_actions >= 1 and description.info.recent_actions:
                    action_result = description.info.recent_actions[-1].action
                elif asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError(f"schedule never fired: {description.info}")
                else:
                    await asyncio.sleep(0.1)
            result = await client.get_workflow_handle(
                action_result.workflow_id, result_type=ScheduledSimulationResult
            ).result()
            description = await handle.describe()
            print(f"Schedule {handle.id} fired (actions so far: {description.info.num_actions}).")
            print(f"  scenario:       {result.scenario}")
            print(f"  expected_value: {result.expected_value} (confidence={result.confidence})")
            print(f"  decision_id:    {result.decision_id} (recorded in decision memory)")
        finally:
            await handle.delete()


if __name__ == "__main__":
    asyncio.run(main())
