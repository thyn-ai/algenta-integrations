"""Tests for `recipes/structured_denial_handling.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

from recipes.structured_denial_handling import (
    Delivered,
    DeliveryFailed,
    PolicyDenied,
    describe,
    run_structured_denial_handling,
)


async def test_all_four_real_outcome_paths_map_to_their_typed_outcomes(stub_server: str) -> None:
    outcomes = await run_structured_denial_handling(stub_server)

    delivered = outcomes["delivered"]
    assert isinstance(delivered, Delivered)
    assert delivered.receipt.is_delivered()
    assert delivered.receipt.response_code == 200

    failed_delivery = outcomes["failed_delivery"]
    assert isinstance(failed_delivery, DeliveryFailed)
    assert failed_delivery.receipt.execution_status == "failed"
    assert failed_delivery.receipt.response_code == 503

    duplicate = outcomes["duplicate"]
    assert isinstance(duplicate, PolicyDenied)
    assert duplicate.denial.gate == "idempotency"
    assert duplicate.denial.override_hint is not None

    low_confidence = outcomes["low_confidence"]
    assert isinstance(low_confidence, PolicyDenied)
    assert low_confidence.denial.gate == "confidence"


async def test_describe_renders_a_distinct_operator_summary_per_path(stub_server: str) -> None:
    outcomes = await run_structured_denial_handling(stub_server)

    assert "delivered to" in describe(outcomes["delivered"])
    assert "alert the webhook owner" in describe(outcomes["failed_delivery"])
    assert "already delivered" in describe(outcomes["duplicate"])
    assert "not retryable" in describe(outcomes["low_confidence"])
