"""Recipe 4 tests: a real Temporal Schedule (cron spec) fires the governed simulation workflow;
the run's result lands in decision memory.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.scheduled_governed_simulation import (
    ScheduledSimulationResult,
    ScheduledSimulationWorkflow,
)
from temporalio.client import Schedule, ScheduleActionStartWorkflow, ScheduleSpec

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_schedule_fires_the_governed_simulation_workflow(temporal_env, engine_state) -> None:
    # The real-time dev server: the time-skipping test server does not implement the schedule
    # RPCs ("CreateSchedule is unimplemented"), and `trigger_immediately` means the run fires
    # in real seconds anyway, so no time skipping is needed here.
    base_url, engine = engine_state
    async with workflow_worker(
        temporal_env, [ScheduledSimulationWorkflow], base_url=base_url, profile="govern"
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
            # trigger_immediately fires the action asynchronously; the action's workflow id is
            # the configured one *plus a timestamp suffix* (Temporal's schedule semantics).
            # Poll the schedule for the action, then wait for the run's result in bounded
            # attempts -- a slow/constrained runner must get a loud, diagnosed failure rather
            # than a silent stall.
            description = None
            action_result = None
            deadline = asyncio.get_event_loop().time() + 60.0
            while action_result is None:
                description = await handle.describe()
                if description.info.num_actions >= 1 and description.info.recent_actions:
                    action_result = description.info.recent_actions[-1].action
                elif asyncio.get_event_loop().time() > deadline:
                    raise AssertionError(f"schedule never fired: {description.info}")
                else:
                    await asyncio.sleep(0.25)
            assert action_result is not None

            result = None
            last_error: BaseException | None = None
            for _attempt in range(3):
                try:
                    result = await asyncio.wait_for(
                        client.get_workflow_handle(
                            action_result.workflow_id, result_type=ScheduledSimulationResult
                        ).result(),
                        timeout=60,
                    )
                    break
                except (TimeoutError, Exception) as error:  # noqa: BLE001 - diagnosed below
                    last_error = error
            if result is None:
                run_status = await client.get_workflow_handle(action_result.workflow_id).describe()
                raise AssertionError(
                    f"schedule-fired run {action_result.workflow_id} never completed "
                    f"(status: {run_status.status}); last error: {last_error!r}"
                )
        finally:
            await handle.delete()

    assert result.scenario == "eurusd-overnight"
    assert result.expected_value == float((len("eurusd-overnight") * 13) % 97)
    assert result.decision_id == "decision-nightly-eurusd-overnight"
    # The scheduled run really recorded itself in decision memory.
    assert any(d["decision_id"] == result.decision_id for d in engine.logged_decisions)
    assert engine.delivered_decision_ids == set()  # govern profile: nothing executed


async def _result_with_retries(client, task_queue: str, scenario: str) -> ScheduledSimulationResult:
    """Run the workflow standalone and fetch its result in bounded attempts, with a diagnosed
    failure (workflow status) rather than a bare timeout."""
    handle = await client.start_workflow(
        ScheduledSimulationWorkflow.run,
        scenario,
        id=f"wf-{uuid.uuid4().hex}",
        task_queue=task_queue,
    )
    last_error: BaseException | None = None
    for _attempt in range(3):
        try:
            return await asyncio.wait_for(
                client.get_workflow_handle(handle.id, result_type=ScheduledSimulationResult).result(),
                timeout=60,
            )
        except (TimeoutError, Exception) as error:  # noqa: BLE001 - diagnosed below
            last_error = error
    status = await handle.describe()
    raise AssertionError(f"standalone run {handle.id} never completed (status: {status.status}); last error: {last_error!r}")


async def test_simulation_workflow_runs_standalone_too(temporal_env, engine_state) -> None:
    base_url, _engine = engine_state
    async with workflow_worker(
        temporal_env, [ScheduledSimulationWorkflow], base_url=base_url, profile="govern"
    ) as (client, task_queue):
        result = await _result_with_retries(
            client,
            task_queue,
            "spx-rebalance",
        )
    assert result.scenario == "spx-rebalance"
    assert result.confidence == 0.87
