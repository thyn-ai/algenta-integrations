"""Recipe 8 tests: the batch pipeline fans out simulate activities, aggregates them
deterministically, ranks them deterministically, and the measured-speed harness agrees with
itself across variants.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from recipes._kernels import KERNEL_SOURCE, naive_rank_documents, rank_documents
from recipes.batch_simulation_pipeline import (
    SCENARIO_BRIEFS,
    BatchSimulationWorkflow,
    RankedBatchReport,
    SequentialSimulationWorkflow,
    _run_ranked_batch,
)
from temporal_algenta.types import BatchSimulationReport

from .helpers import workflow_worker

pytestmark = pytest.mark.asyncio(loop_scope="module")

SCENARIOS = list(SCENARIO_BRIEFS)


def _expected_value(scenario: str) -> float:
    # The demo engine's documented deterministic derivation.
    return float((len(scenario) * 13) % 97)


async def test_batch_simulation_fans_out_and_aggregates(temporal_env, engine_state) -> None:
    base_url, _engine = engine_state
    async with workflow_worker(
        temporal_env, [BatchSimulationWorkflow], base_url=base_url, profile="observe"
    ) as (client, task_queue):
        report = await asyncio.wait_for(client.execute_workflow(
            BatchSimulationWorkflow.run,
            SCENARIOS,
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=BatchSimulationReport,
        ), timeout=150)

    assert report.scenario_count == len(SCENARIOS)
    assert report.succeeded == len(SCENARIOS)
    assert report.expected_values == {scenario: _expected_value(scenario) for scenario in SCENARIOS}
    expected_mean = sum(_expected_value(s) for s in SCENARIOS) / len(SCENARIOS)
    assert report.mean_expected_value == expected_mean


async def test_batch_simulation_empty_input_is_deterministic(temporal_env, engine_state) -> None:
    base_url, _engine = engine_state
    async with workflow_worker(
        temporal_env, [BatchSimulationWorkflow], base_url=base_url, profile="observe"
    ) as (client, task_queue):
        report = await asyncio.wait_for(client.execute_workflow(
            BatchSimulationWorkflow.run,
            [],
            id=f"wf-{uuid.uuid4().hex}",
            task_queue=task_queue,
            result_type=BatchSimulationReport,
        ), timeout=150)
    assert report.scenario_count == 0
    assert report.succeeded == 0
    assert report.expected_values == {}
    assert report.mean_expected_value == 0.0


async def test_parallel_and_sequential_variants_produce_identical_reports(temporal_env, engine_state) -> None:
    """The speed harness: both workflow variants must return the same deterministic report
    (the speedup claim is only meaningful if the results are identical), and the ranking stage
    must be fully deterministic."""
    base_url, _engine = engine_state
    workflows = [BatchSimulationWorkflow, SequentialSimulationWorkflow]
    async with workflow_worker(temporal_env, workflows, base_url=base_url, profile="observe") as (
        client,
        task_queue,
    ):
        report, parallel_seconds, sequential_seconds = await asyncio.wait_for(
            _run_ranked_batch(client, task_queue, SCENARIOS), timeout=120
        )

    assert isinstance(report, RankedBatchReport)
    assert report.simulation.expected_values == {s: _expected_value(s) for s in SCENARIOS}
    # Measured durations are positive real numbers (no direction asserted -- wall-clock
    # inequalities are exactly the kind of flaky test this suite avoids).
    assert parallel_seconds > 0
    assert sequential_seconds > 0
    # The ranking is deterministic: recompute and compare exactly.
    briefs = [SCENARIO_BRIEFS[s] for s in SCENARIOS]
    expected_ranking = [(SCENARIOS[i], score) for i, score in rank_documents("rates volatility gap forex currency overnight", briefs)]
    assert report.ranking == expected_ranking
    assert report.kernel_source == KERNEL_SOURCE


def test_bm25_ranking_is_deterministic_and_theme_relevant() -> None:
    briefs = [SCENARIO_BRIEFS[s] for s in SCENARIOS]
    first = rank_documents("forex currency overnight", briefs)
    second = rank_documents("forex currency overnight", briefs)
    assert first == second
    # The forex-laden brief wins outright, whatever kernel produced the scores.
    top_scenario = SCENARIOS[first[0][0]]
    assert top_scenario == "eurusd-overnight"
    assert first[0][1] > 0
    # A query that matches nothing scores zero everywhere (and ordering falls back to index).
    assert all(score == 0.0 for _i, score in rank_documents("zzz-no-such-term", briefs))
    # The naive baseline runs and returns the same shaped result.
    naive = naive_rank_documents("forex", briefs)
    assert len(naive) == len(briefs)
    assert naive[0][1] >= naive[-1][1]
