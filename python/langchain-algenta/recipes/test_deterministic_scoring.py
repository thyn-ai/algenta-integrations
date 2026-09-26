"""Tests for `recipes/deterministic_scoring.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

import pytest

from recipes.deterministic_scoring import CANDIDATES, run_deterministic_scoring


async def test_candidates_are_ranked_by_the_governed_composite_score(score_stub_server: str) -> None:
    result = await run_deterministic_scoring(score_stub_server, CANDIDATES)

    ranking = result["ranking"]
    assert len(ranking) == len(CANDIDATES)
    scores = [entry["score"] for entry in ranking]
    assert scores == sorted(scores, reverse=True)
    assert result["winner"] == ranking[0]["candidate"]
    assert result["winner_score"] == ranking[0]["score"]
    for entry in ranking:
        assert 0.0 <= entry["score"] <= 1.0
        assert set(entry["score_breakdown"]) == {
            "expected_value_weight",
            "downside_risk_weight",
            "normalized_expected_value",
            "one_minus_probability_of_loss",
        }


async def test_scoring_is_deterministic_across_runs_and_the_eval_decision_is_logged(score_stub_server: str) -> None:
    first = await run_deterministic_scoring(score_stub_server, CANDIDATES)
    second = await run_deterministic_scoring(score_stub_server, CANDIDATES)

    assert first["ranking"] == second["ranking"]
    assert first["winner"] == second["winner"]
    assert first["decision_id"] == second["decision_id"] == f"decision-eval-select:{first['winner']}"


async def test_a_below_threshold_batch_is_gated_not_shipped(score_stub_server: str) -> None:
    # A threshold of 1.0 is unreachable (composite scores are strictly below 1 for any
    # candidate with a nonzero probability of loss), so the batch must gate.
    result = await run_deterministic_scoring(score_stub_server, CANDIDATES, pass_threshold=1.0)
    assert result["gated"] is True


async def test_an_empty_candidate_set_is_rejected(score_stub_server: str) -> None:
    with pytest.raises(ValueError, match="at least one candidate"):
        await run_deterministic_scoring(score_stub_server, [])
