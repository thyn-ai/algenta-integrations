"""Tests for `recipes/idempotent_tool_retry.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

import pytest
from langchain_algenta import AlgentaExecutionBlocked, create_algenta_tools
from tests.stub_server import LOW_CONFIDENCE_DECISION_ID

from recipes._support import tool_by_name
from recipes.idempotent_tool_retry import (
    AlreadyDelivered,
    Delivered,
    execute_idempotent,
    run_idempotent_tool_retry,
)


async def test_transport_failures_are_retried_and_duplicates_dedup_at_most_once(stub_server: str) -> None:
    result = await run_idempotent_tool_retry(stub_server, decision_id="decision-1", webhook_url="https://example.com/hook")

    retried = result["retried"]
    assert isinstance(retried, Delivered)
    assert retried.attempts == 2  # one transport failure, then success
    assert retried.receipt.is_delivered()

    deduped = result["deduped"]
    assert isinstance(deduped, AlreadyDelivered)
    assert deduped.decision_id == "decision-1"
    assert deduped.attempts == 1  # the idempotency gate answered on the first attempt

    # The whole run delivered exactly once: at-most-once held across both scenarios.
    assert result["delivered_count"] == 1


async def test_a_policy_denial_is_never_retried(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = tool_by_name(tools, "execute_decision")

    attempts = 0

    async def _counting(**kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        return await execute_decision.ainvoke(kwargs)

    from langchain_core.tools import StructuredTool

    counting_tool = StructuredTool.from_function(
        name="execute_decision",
        description=execute_decision.description,
        args_schema=execute_decision.args_schema if isinstance(execute_decision.args_schema, dict) else None,
        coroutine=_counting,
    )

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await execute_idempotent(counting_tool, decision_id=LOW_CONFIDENCE_DECISION_ID, webhook_url="https://x")
    assert exc_info.value.gate == "confidence"
    assert attempts == 1  # policy said no; no retry happened
