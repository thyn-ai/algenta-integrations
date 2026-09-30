"""End-to-end scenarios: real `AlgentaToolset` -> real `fastmcp.Client` -> real HTTP socket ->
the stub Algenta server in `tests/stub_server.py`.

These tests use the real MCP transport. They do not instantiate a smolagents `Agent` because
that would require a model, but they do exercise the full toolset-to-wire path and the exact
primitives a smolagents agent uses: tool discovery, schema scrubbing, `forward()` calls, and
error surfacing.
"""

from __future__ import annotations

import pytest
from smolagents import AgentToolExecutionError
from smolagents_algenta import AlgentaToolset, ExecutionReceipt

from .stub_server import LOW_CONFIDENCE_DECISION_ID, NON_ENVELOPE_RESULT, RISK_FLOOR_DECISION_ID


def test_successful_read_only_recommendation_on_observe_profile(stub_server: str) -> None:
    with AlgentaToolset(base_url=stub_server, profile="observe") as toolset:
        recommend = next(t for t in toolset.tools if t.name == "recommend")
        result = recommend.forward(scenario="expand-warehouse")

        # recommend is freeform, not an execute_decision-shaped envelope -- passes through unchanged.
        assert result["recommended_action"] == "hold"


def test_observe_profile_agent_cannot_even_call_execute_decision(stub_server: str) -> None:
    with AlgentaToolset(base_url=stub_server, profile="observe") as toolset:
        assert "execute_decision" not in {t.name for t in toolset.tools}


def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't an execute_decision-shaped envelope at all.
    with AlgentaToolset(base_url=stub_server, profile="observe") as toolset:
        get_contract = next(t for t in toolset.tools if t.name == "get_contract")
        result = get_contract.forward()

        assert result == NON_ENVELOPE_RESULT


def test_execute_decision_success_returns_a_typed_execution_receipt(stub_server: str) -> None:
    with AlgentaToolset(base_url=stub_server, profile="execute") as toolset:
        execute = next(t for t in toolset.tools if t.name == "execute_decision")
        result = execute.forward(decision_id="dec-first-call", webhook_url="https://example.com/hook")

        assert isinstance(result, ExecutionReceipt)
        assert result.decision_id == "dec-first-call"
        assert result.webhook_url == "https://example.com/hook"
        assert result.execution_status == "delivered"
        assert result.is_delivered()
        assert result.safety_overridden is False


def test_execute_decision_denied_by_the_confidence_gate(stub_server: str) -> None:
    with AlgentaToolset(base_url=stub_server, profile="execute") as toolset:
        execute = next(t for t in toolset.tools if t.name == "execute_decision")

        with pytest.raises(AgentToolExecutionError, match="confidence"):
            execute.forward(decision_id=LOW_CONFIDENCE_DECISION_ID, webhook_url="https://example.com/hook")


def test_execute_decision_denied_by_the_risk_floor_gate(stub_server: str) -> None:
    with AlgentaToolset(base_url=stub_server, profile="execute") as toolset:
        execute = next(t for t in toolset.tools if t.name == "execute_decision")

        with pytest.raises(AgentToolExecutionError, match="risk_floor"):
            execute.forward(decision_id=RISK_FLOOR_DECISION_ID, webhook_url="https://example.com/hook")


def test_execute_decision_denied_by_the_idempotency_gate_on_a_repeat_call(stub_server: str) -> None:
    # The idempotency gate is the one that fires from ordinary use, not a magic sentinel: call
    # the same decision_id twice without force and the second call is blocked.
    with AlgentaToolset(base_url=stub_server, profile="execute") as toolset:
        execute = next(t for t in toolset.tools if t.name == "execute_decision")
        args = {"decision_id": "dec-repeat", "webhook_url": "https://example.com/hook"}

        first = execute.forward(**args)
        assert isinstance(first, ExecutionReceipt)

        with pytest.raises(AgentToolExecutionError, match="idempotency"):
            execute.forward(**args)
