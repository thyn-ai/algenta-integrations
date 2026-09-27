"""Tests for `temporal_algenta.activities.AlgentaActivities`, run through Temporal's real
`ActivityEnvironment` against the stub engine -- the same activity objects a `Worker` would
register, exercised with the same payloads a workflow would send.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporal_algenta.activities import AlgentaActivities
from temporal_algenta.types import (
    PROFILE_DENIED_ERROR_TYPE,
    TOOL_ERROR_TYPE,
    UNEXPECTED_RESULT_ERROR_TYPE,
    DenialDetails,
    ExecuteDecisionInput,
    ExecutionReceiptData,
    LogDecisionInput,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from .helpers import run_activity_resilient
from .stub_server import (
    BELOW_RISK_FLOOR_DECISION_ID,
    FAILED_DELIVERY_DECISION_ID,
    FLAKY_DATASET,
    LOW_CONFIDENCE_DECISION_ID,
)


@pytest.fixture
def activity_env() -> ActivityEnvironment:
    return ActivityEnvironment()


async def test_freeform_activities_return_engine_payloads(activity_env: ActivityEnvironment, stub_server) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="govern")

    contract = await run_activity_resilient(activity_env, algenta.get_contract)
    assert contract["engine_version"] == "1.4.0"

    rows = await run_activity_resilient(activity_env, algenta.query_data, "positions")
    assert rows["dataset"] == "positions"

    simulation = await run_activity_resilient(activity_env, algenta.simulate, "spx-rebalance")
    assert simulation["scenario"] == "spx-rebalance"

    recommendation = await run_activity_resilient(activity_env, algenta.recommend, "spx-rebalance")
    assert recommendation["recommended_action"] == "hold"

    plan = await run_activity_resilient(activity_env, algenta.plan_decision, "spx-rebalance")
    assert plan["plan_id"] == "plan-spx-rebalance"

    logged = await run_activity_resilient(activity_env,
        algenta.log_decision, LogDecisionInput(chosen_action="hold", rationale="test", note="n")
    )
    assert logged["decision_id"] == "decision-hold"
    assert logged["rationale"] == "test"
    assert logged["note"] == "n"


async def test_execute_decision_returns_typed_receipt_data(activity_env: ActivityEnvironment, stub_server) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="execute")
    receipt = await run_activity_resilient(activity_env,
        algenta.execute_decision,
        ExecuteDecisionInput(decision_id="decision-hold", webhook_url="https://ops.example.com/hooks/hold"),
    )
    assert isinstance(receipt, ExecutionReceiptData)
    assert receipt.is_delivered()
    assert receipt.response_code == 200
    assert receipt.policy_snapshot_id == "policy-snap-1"
    assert not receipt.safety_overridden


async def test_execute_decision_denial_becomes_typed_non_retryable_application_error(
    activity_env: ActivityEnvironment, stub_server
) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="execute")
    with pytest.raises(ApplicationError) as exc_info:
        await run_activity_resilient(activity_env,
            algenta.execute_decision,
            ExecuteDecisionInput(decision_id=BELOW_RISK_FLOOR_DECISION_ID, webhook_url="http://x"),
        )
    error = exc_info.value
    assert error.type == "execution_blocked_risk_floor"
    assert error.non_retryable is True
    denial = DenialDetails.from_json_dict(error.details[0])
    assert denial is not None
    assert denial.gate == "risk_floor"
    assert denial.override_hint is not None


async def test_execute_decision_failed_delivery_is_a_receipt_not_a_denial(
    activity_env: ActivityEnvironment, stub_server
) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="execute")
    receipt = await run_activity_resilient(activity_env,
        algenta.execute_decision,
        ExecuteDecisionInput(decision_id=FAILED_DELIVERY_DECISION_ID, webhook_url="http://down"),
    )
    assert receipt.execution_status == "failed"
    assert receipt.response_code == 503
    assert not receipt.is_delivered()


async def test_operator_override_flows_through_to_the_engine(activity_env: ActivityEnvironment, stub_server) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="execute")
    receipt = await run_activity_resilient(activity_env,
        algenta.execute_decision,
        ExecuteDecisionInput(
            decision_id=LOW_CONFIDENCE_DECISION_ID, webhook_url="http://x", override_safety=True
        ),
    )
    assert receipt.safety_overridden is True


async def test_profile_violation_is_a_typed_non_retryable_refusal(
    activity_env: ActivityEnvironment, stub_server
) -> None:
    base_url, engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="govern")
    with pytest.raises(ApplicationError) as exc_info:
        await run_activity_resilient(activity_env,
            algenta.execute_decision,
            ExecuteDecisionInput(decision_id="decision-hold", webhook_url="http://x"),
        )
    assert exc_info.value.type == PROFILE_DENIED_ERROR_TYPE
    assert exc_info.value.non_retryable is True
    assert engine.execute_attempts == {}


async def test_generic_tool_error_is_a_retryable_application_error(
    activity_env: ActivityEnvironment, stub_server
) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="observe")
    with pytest.raises(ApplicationError) as exc_info:
        await run_activity_resilient(activity_env, algenta.query_data, FLAKY_DATASET)
    assert exc_info.value.type == TOOL_ERROR_TYPE
    assert exc_info.value.non_retryable is False


async def test_all_activities_registers_the_seven_contract_tools(stub_server) -> None:
    base_url, _engine = stub_server
    algenta = AlgentaActivities(base_url=base_url, profile="observe")
    names = [getattr(fn, "__temporal_activity_definition").name for fn in algenta.all_activities()]
    assert sorted(names) == [
        "execute_decision",
        "get_contract",
        "log_decision",
        "plan_decision",
        "query_data",
        "recommend",
        "simulate",
    ]


def test_unknown_profile_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        AlgentaActivities(profile="admin")  # type: ignore[arg-type]


class _FakeClient:
    """Test seam for the two defensive shape-check branches: an MCP-free stand-in for
    `AlgentaMcpClient` returning payloads no real engine would send. The wire behavior itself
    is covered by the stub-engine tests above."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return self._payload


async def test_execute_decision_rejects_a_non_receipt_payload(
    activity_env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    algenta = AlgentaActivities(base_url="http://unused", profile="execute")
    monkeypatch.setattr(algenta, "_client", lambda: _FakeClient({"not": "a receipt"}))
    with pytest.raises(ApplicationError) as exc_info:
        await run_activity_resilient(activity_env,
            algenta.execute_decision,
            ExecuteDecisionInput(decision_id="decision-x", webhook_url="http://x"),
        )
    assert exc_info.value.type == UNEXPECTED_RESULT_ERROR_TYPE
    assert exc_info.value.non_retryable is True


async def test_dict_tools_reject_a_non_dict_payload(
    activity_env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    algenta = AlgentaActivities(base_url="http://unused", profile="observe")
    monkeypatch.setattr(algenta, "_client", lambda: _FakeClient(["not", "a", "dict"]))
    with pytest.raises(ApplicationError) as exc_info:
        await run_activity_resilient(activity_env, algenta.simulate, "spx")
    assert exc_info.value.type == UNEXPECTED_RESULT_ERROR_TYPE
    assert exc_info.value.non_retryable is True
