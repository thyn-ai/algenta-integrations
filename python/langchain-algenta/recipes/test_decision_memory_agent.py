"""Tests for `recipes/decision_memory_agent.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

from langchain_algenta import create_algenta_tools

from recipes.decision_memory_agent import MEMORY_READ_TOOLS, run_decision_memory_agent


async def test_the_feedback_loop_closes_and_updates_the_accuracy_summary(memory_stub_server: str) -> None:
    result = await run_decision_memory_agent(memory_stub_server, chosen_action="expand-eu", actual_outcome=47.5)

    assert result["decision_id"] == "decision-expand-eu"
    # The base stub's log_decision fixture fixes expected_value at 42.0, so the recorded
    # outcome 47.5 yields outcome_delta 5.5.
    assert result["outcome_delta"] == 5.5

    record = result["memory_record"]
    assert record["decision_id"] == "decision-expand-eu"
    assert record["actual_outcome"] == 47.5
    assert record["outcome_delta"] == 5.5

    # The stub engine ships with two seeded decisions that already have outcomes; this run
    # closes the loop on one more.
    assert result["with_outcome_total"] == 3
    summary = result["accuracy_summary"]
    assert summary["with_outcome"] == 3
    # mean of the three outcome deltas: seeded 3.0 and -7.5, plus this run's 5.5.
    assert summary["mean_outcome_delta"] == round((3.0 - 7.5 + 5.5) / 3, 2)


async def test_recording_the_same_outcome_twice_converges(memory_stub_server: str) -> None:
    first = await run_decision_memory_agent(memory_stub_server, chosen_action="expand-eu", actual_outcome=47.5)
    second = await run_decision_memory_agent(memory_stub_server, chosen_action="expand-eu", actual_outcome=47.5)

    assert first["with_outcome_total"] == second["with_outcome_total"]
    assert first["outcome_delta"] == second["outcome_delta"]


async def test_memory_reads_never_appear_in_the_model_facing_tool_list(memory_stub_server: str) -> None:
    govern_tools = await create_algenta_tools(base_url=memory_stub_server, profile="govern")
    model_facing = {tool.name for tool in govern_tools}
    for name in MEMORY_READ_TOOLS:
        assert name not in model_facing

    full_tools = await create_algenta_tools(base_url=memory_stub_server, profile="full")
    ops_facing = {tool.name for tool in full_tools}
    for name in MEMORY_READ_TOOLS:
        assert name in ops_facing
