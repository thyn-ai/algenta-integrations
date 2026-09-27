"""Recipe 4 tests: a real Temporal Schedule (cron spec) fires the governed simulation workflow;
the run's result lands in decision memory.
"""

from __future__ import annotations

import asyncio
import uuid

from recipes.scheduled_governed_simulation import (
    ScheduledSimulationResult,
    ScheduledSimulationWorkflow,
)
from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleSpec

from .helpers import workflow_worker


async def test_schedule_fires_the_governed_simulation_workflow(temporal_env_realtime, stub_server) -> None:
    # The real-time dev server: the time-skipping test server does not implement the schedule
    # RPCs ("CreateSchedule is unimplemented"), and `trigger_immediately` means the run fires
    # in real seconds anyway, so no time skipping is needed here.
    base_url, engine = stub_server
    async with workflow_worker(
        temporal_env_realtime, [ScheduledSimulationWorkflow], base_url=base_url, profile="govern"
    ) as (client, task_queue):
        workflow_id = f"nightly-sim-{uuid.uuid4().hex}"
        handle = await client.create_schedule(
            f"sched-{uuid.uuid4().hex}",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ScheduledSimulationWorkflow.run,
                    "eurusd-overnight",
                    id=workflow_id,
                    task_queue=task_queue,
                ),
                spec=ScheduleSpec(cron_expressions=["0 2 * * *"]),
            ),
            trigger_immediately=True,
        )
        try:
            # trigger_immediately fires the action asynchronously; wait for the schedule to
            # actually record it before fetching the run's result. The action's workflow id is
            # the configured one *plus a timestamp suffix* (Temporal's schedule semantics), so
            # the run's real id comes from the schedule's recent_actions, not from the id we
            # configured.
            deadline = asyncio.get_event_loop().time() + 30.0
            while True:
                description = await handle.describe()
                if description.info.num_actions >= 1 and description.info.recent_actions:
                    break
                if asyncio.get_event_loop().time() > deadline:
                    raise AssertionError(f"schedule never fired: {description.info}")
                await asyncio.sleep(0.1)
            action_result = description.info.recent_actions[-1].action
            assert action_result is not None
            result = await asyncio.wait_for(
                client.get_workflow_handle(action_result.workflow_id, result_type=ScheduledSimulationResult).result(),
                timeout=150,
            )
        finally:
            await handle.delete()

    assert result.scenario == "eurusd-overnight"
    assert result.expected_value == float((len("eurusd-overnight") * 13) % 97)
    assert result.decision_id == "decision-nightly-eurusd-overnight"
    # The scheduled run really recorded itself in decision memory.
    assert any(d["decision_id"] == result.decision_id for d in engine.logged_decisions)
    assert engine.delivered_decision_ids == set()  # govern profile: nothing executed


async def test_simulation_workflow_runs_standalone_too(temporal_env, stub_server) -> None:
    base_url, _engine = stub_server
    async with workflow_worker(
        temporal_env, [ScheduledSimulationWorkflow], base_url=base_url, profile="govern"
    ) as (client, task_queue):
        result = await asyncio.wait_for(client.execute_workflow(
            ScheduledSimulationWorkflow.run,
            "spx-rebalance",
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=ScheduledSimulationResult,
        ), timeout=150)
    assert result.scenario == "spx-rebalance"
    assert result.confidence == 0.87
