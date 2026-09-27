"""Recipe 9 tests: the governed agent workflow advertises only the profile's tools; refused
steps never reach the engine, and the full plan completes under `execute`.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes.agent_workflow_governed_tools import AgentRunReport, GovernedAgentWorkflow

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")

SCENARIO = "spx-rebalance"
WEBHOOK = "https://ops.example.com/hooks/rebalance"


async def _run_agent(client, task_queue: str, profile: str) -> AgentRunReport:
    return await asyncio.wait_for(
        client.execute_workflow(
            GovernedAgentWorkflow.run,
            args=[SCENARIO, WEBHOOK, profile],
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=AgentRunReport,
        ),
        timeout=150,
    )


async def test_agent_under_execute_profile_completes_the_full_plan(temporal_env, engine_state) -> None:
    base_url, engine = engine_state
    async with workflow_worker(
        temporal_env, [GovernedAgentWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        report = await _run_agent(client, task_queue, "execute")

    assert [(step.tool, step.status) for step in report.steps] == [
        ("recommend", "ok"),
        ("plan_decision", "ok"),
        ("log_decision", "ok"),
        ("execute_decision", "ok"),
    ]
    assert report.executed_decision_id == f"decision-agent-{SCENARIO}"
    assert report.receipt_status == "delivered"
    assert engine.delivered_decision_ids == {f"decision-agent-{SCENARIO}"}


async def test_agent_under_govern_profile_plans_but_never_acts(temporal_env, engine_state) -> None:
    base_url, engine = engine_state
    async with workflow_worker(
        temporal_env, [GovernedAgentWorkflow], base_url=base_url, profile="execute"
    ) as (client, task_queue):
        report = await _run_agent(client, task_queue, "govern")

    statuses = {step.tool: step.status for step in report.steps}
    assert statuses == {
        "recommend": "ok",
        "plan_decision": "ok",
        "log_decision": "ok",
        "execute_decision": "refused_by_profile",
    }
    assert report.executed_decision_id is None
    assert report.receipt_status is None
    # The refusal happened before any activity: the engine saw the decision get logged (govern
    # allows that) but never an execute attempt.
    assert engine.delivered_decision_ids == set()
    assert engine.execute_attempts == {}
    assert [d["decision_id"] for d in engine.logged_decisions] == [f"decision-agent-{SCENARIO}"]


async def test_agent_under_observe_profile_only_reads(temporal_env, engine_state) -> None:
    base_url, engine = engine_state
    async with workflow_worker(
        temporal_env, [GovernedAgentWorkflow], base_url=base_url, profile="observe"
    ) as (client, task_queue):
        report = await _run_agent(client, task_queue, "observe")

    statuses = {step.tool: step.status for step in report.steps}
    assert statuses == {
        "recommend": "ok",
        "plan_decision": "refused_by_profile",
        "log_decision": "refused_by_profile",
        "execute_decision": "refused_by_profile",
    }
    assert engine.execute_attempts == {}
    assert engine.logged_decisions == []
