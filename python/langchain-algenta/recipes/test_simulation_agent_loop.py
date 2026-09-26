"""Tests for `recipes/simulation_agent_loop.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

from recipes.simulation_agent_loop import run_simulation_agent_loop


async def test_the_agent_simulates_and_recommends_before_answering(stub_server: str) -> None:
    result = await run_simulation_agent_loop(stub_server, "expand-eu")

    # The stub engine's deterministic fixtures for both observe-profile tools.
    assert result["simulation"] == {"scenario": "expand-eu", "expected_value": 42.0}
    assert result["recommendation"] == {"scenario": "expand-eu", "recommended_action": "hold", "confidence": 0.87}
    assert "42.0" in result["final_message"]


async def test_the_loop_runs_on_the_read_only_observe_profile(stub_server: str) -> None:
    result = await run_simulation_agent_loop(stub_server, "expand-eu")

    assert result["tool_names"] == ["get_contract", "query_data", "recommend", "simulate"]
