"""Recipe 8 -- Batch simulation pipeline, with the accelerated hot paths measured.

Temporal's parallel fan-out pattern -- `asyncio.gather` over activity handles inside a
workflow -- applied to the most natural batch job in a decision platform: simulate a whole
portfolio of scenarios at once, then reduce the envelopes into one deterministic report. The
fan-out is parallel (each `simulate` is its own durable, independently-retried activity), the
aggregation is pure workflow code (deterministic under replay -- input order is preserved, no
clocks, no randomness), and every per-scenario result is the engine's own simulation envelope.

Two compute-heavy paths, both measured live in `main()`:

1. **Simulation loop.** The parallel fan-out vs. the naive sequential loop (same scenarios,
   same engine, same run) -- the durable-execution speedup, printed with real wall-clock
   numbers.
2. **Retrieval ranking.** The scenarios are then ranked against the portfolio theme with BM25
   (`recipes/_kernels.py`): the `bm25_mojo` kernel when it's importable (an optional,
   undeclared runtime extra -- not yet on PyPI as of this writing), otherwise the
   deterministic pure-Python stand-in that ships with the recipe. `main()` prints which one
   ran plus the measured throughput, next to the naive token-overlap baseline. The kernel is a
   speed upgrade for this step, never a correctness one: both paths compute the same Okapi
   BM25 ranking.

Run it: `uv run python -m recipes.batch_simulation_pipeline`
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_algenta.activities import AlgentaActivities
    from temporal_algenta.types import BatchSimulationReport

#: Deterministic per-scenario briefs the retrieval stage ranks (scenario name -> the text a
#: portfolio theme is matched against). Fixed content keeps the whole pipeline reproducible.
SCENARIO_BRIEFS: Final[dict[str, str]] = {
    "eurusd-overnight": "euro dollar currency overnight gap forex FX rates volatility",
    "spx-rebalance": "equity index rebalance S&P 500 stocks allocation drift",
    "treasury-roll": "treasury bonds roll yield curve duration rates fixed income",
    "crypto-weekend-gap": "crypto bitcoin weekend liquidity gap volatility digital assets",
    "gold-hedge": "gold commodities hedge inflation safe haven metals",
    "oil-inventory": "crude oil inventory energy commodities supply demand",
}
PORTFOLIO_THEME: Final = "rates volatility gap forex currency overnight"


@dataclass
class RankedBatchReport:
    """The batch result: per-scenario simulation envelopes plus the BM25 relevance ranking."""

    simulation: BatchSimulationReport
    ranking: list[tuple[str, float]] = field(default_factory=list)
    """`(scenario, score)` pairs, most relevant first (ties broken alphabetically)."""
    kernel_source: str = ""
    """Which BM25 implementation produced the ranking (see `recipes/_kernels.py`)."""


def _aggregate(scenarios: list[str], envelopes: list[dict]) -> BatchSimulationReport:
    """Deterministic reduction of per-scenario envelopes (input order preserved)."""
    expected_values = {
        scenario: float(envelope["expected_value"])
        for scenario, envelope in zip(scenarios, envelopes, strict=True)
    }
    return BatchSimulationReport(
        scenario_count=len(scenarios),
        succeeded=len(envelopes),
        expected_values=expected_values,
        mean_expected_value=sum(expected_values.values()) / len(expected_values) if expected_values else 0.0,
    )


@workflow.defn
class BatchSimulationWorkflow:
    """Fan out `simulate` over every scenario in parallel, then aggregate deterministically."""

    @workflow.run
    async def run(self, scenarios: list[str]) -> BatchSimulationReport:
        envelopes = await asyncio.gather(
            *(
                workflow.execute_activity(
                    AlgentaActivities.simulate,
                    scenario,
                    start_to_close_timeout=timedelta(seconds=60),
                )
                for scenario in scenarios
            )
        )
        # `asyncio.gather` preserves input order, so the report is byte-for-byte reproducible
        # for the same scenario list -- no clocks, no randomness, nothing replay-unsafe.
        return _aggregate(scenarios, list(envelopes))


@workflow.defn
class SequentialSimulationWorkflow:
    """The naive baseline: the same scenarios simulated one activity at a time, in order.

    Exists so `main()` can measure the fan-out's speedup honestly -- same worker, same engine,
    same run.
    """

    @workflow.run
    async def run(self, scenarios: list[str]) -> BatchSimulationReport:
        envelopes: list[dict] = []
        for scenario in scenarios:
            envelopes.append(
                await workflow.execute_activity(
                    AlgentaActivities.simulate,
                    scenario,
                    start_to_close_timeout=timedelta(seconds=60),
                )
            )
        return _aggregate(scenarios, envelopes)


async def _run_ranked_batch(client, task_queue: str, scenarios: list[str]) -> tuple[RankedBatchReport, float, float]:
    """Run both variants back-to-back and the BM25 ranking; return the report and the two
    measured wall-clock durations (parallel, sequential)."""
    from recipes._kernels import KERNEL_SOURCE, rank_documents

    started = time.perf_counter()
    report = await client.execute_workflow(
        BatchSimulationWorkflow.run,
        scenarios,
        id=f"batch-sim-{uuid.uuid4().hex[:8]}",
        task_queue=task_queue,
        result_type=BatchSimulationReport,
    )
    parallel_seconds = time.perf_counter() - started

    started = time.perf_counter()
    baseline = await client.execute_workflow(
        SequentialSimulationWorkflow.run,
        scenarios,
        id=f"seq-sim-{uuid.uuid4().hex[:8]}",
        task_queue=task_queue,
        result_type=BatchSimulationReport,
    )
    sequential_seconds = time.perf_counter() - started
    assert baseline == report  # same scenarios, same engine: identical results, different speed

    briefs = [SCENARIO_BRIEFS[scenario] for scenario in scenarios]
    ranked = rank_documents(PORTFOLIO_THEME, briefs)
    ranking = [(scenarios[index], score) for index, score in ranked]
    return (
        RankedBatchReport(simulation=report, ranking=ranking, kernel_source=KERNEL_SOURCE),
        parallel_seconds,
        sequential_seconds,
    )


async def main() -> None:
    from recipes._kernels import naive_rank_documents, rank_documents
    from recipes._runner import recipe_worker

    scenarios = list(SCENARIO_BRIEFS)
    workflows = [BatchSimulationWorkflow, SequentialSimulationWorkflow]
    async with recipe_worker(workflows, profile="observe") as (client, task_queue):
        report, parallel_seconds, sequential_seconds = await _run_ranked_batch(client, task_queue, scenarios)

    print(f"Simulated {report.simulation.succeeded}/{report.simulation.scenario_count} scenarios:")
    for scenario, value in report.simulation.expected_values.items():
        print(f"  {scenario:20s} expected_value={value}")
    print(f"  mean expected value: {report.simulation.mean_expected_value}")

    speedup = sequential_seconds / parallel_seconds if parallel_seconds else float("inf")
    print("\nSpeed, measured live (same worker, same engine, same run):")
    print(f"  parallel fan-out:  {parallel_seconds * 1000:.0f} ms")
    print(f"  sequential loop:   {sequential_seconds * 1000:.0f} ms")
    print(f"  fan-out speedup:   {speedup:.1f}x")

    # The retrieval hot path: rank the scenario briefs against the portfolio theme. Measured
    # against the naive token-overlap baseline -- and the kernel seam is named explicitly.
    reps = 200
    briefs = [SCENARIO_BRIEFS[scenario] for scenario in scenarios] * 25  # 150 docs
    started = time.perf_counter()
    for _ in range(reps):
        rank_documents(PORTFOLIO_THEME, briefs)
    kernel_ms = (time.perf_counter() - started) / reps * 1000
    started = time.perf_counter()
    for _ in range(reps):
        naive_rank_documents(PORTFOLIO_THEME, briefs)
    naive_ms = (time.perf_counter() - started) / reps * 1000
    print(f"\nRetrieval ranking ({len(briefs)} docs x {reps} reps), kernel in use: {report.kernel_source}")
    print(f"  bm25 ({report.kernel_source}): {kernel_ms:.3f} ms/rep")
    print(f"  naive token recount:           {naive_ms:.3f} ms/rep ({naive_ms / kernel_ms:.1f}x slower)")
    if report.kernel_source != "bm25_mojo":
        print("  (bm25-mojo is an optional, undeclared runtime extra -- once it's installed,")
        print("   this same step runs the Mojo kernel; correctness is identical by construction.)")

    print("\nTop-3 scenarios by theme relevance:")
    for scenario, score in report.ranking[:3]:
        print(f"  {scenario:20s} bm25={score:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
