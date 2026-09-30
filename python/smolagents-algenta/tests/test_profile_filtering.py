"""Tool-profile filtering: an `"observe"`-profile toolset must not even *list* execute-tier
tools to the model, not just document them as unavailable.

Uses pre-built smolagents `Tool` objects passed via `AlgentaToolset(wrapped=...)` rather than the
network stub server -- profile filtering is pure tool construction logic and doesn't need a real
MCP round trip to exercise.
"""

from __future__ import annotations

import pytest
from smolagents import Tool
from smolagents_algenta import AlgentaToolset


class _GetContract(Tool):
    name = "get_contract"
    description = "Get the contract."
    inputs = {}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self) -> dict:
        return {"capabilities": []}


class _QueryData(Tool):
    name = "query_data"
    description = "Query data."
    inputs = {"dataset": {"type": "string", "description": "dataset"}}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, dataset: str) -> dict:
        return {"dataset": dataset, "rows": []}


class _Simulate(Tool):
    name = "simulate"
    description = "Simulate."
    inputs = {"scenario": {"type": "string", "description": "scenario"}}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, scenario: str) -> dict:
        return {"scenario": scenario, "expected_value": 1.0}


class _Recommend(Tool):
    name = "recommend"
    description = "Recommend."
    inputs = {"scenario": {"type": "string", "description": "scenario"}}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, scenario: str) -> dict:
        return {"scenario": scenario, "recommended_action": "hold"}


class _PlanDecision(Tool):
    name = "plan_decision"
    description = "Plan."
    inputs = {"scenario": {"type": "string", "description": "scenario"}}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, scenario: str) -> dict:
        return {"summary": "proposed plan", "scenario": scenario}


class _LogDecision(Tool):
    name = "log_decision"
    description = "Log."
    inputs = {"chosen_action": {"type": "string", "description": "action"}}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, chosen_action: str) -> dict:
        return {"decision_id": "dec-1", "chosen_action": chosen_action}


class _ExecuteDecision(Tool):
    name = "execute_decision"
    description = "Execute."
    inputs = {
        "decision_id": {"type": "string", "description": "id"},
        "webhook_url": {"type": "string", "description": "url"},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, decision_id: str, webhook_url: str) -> dict:
        return {"decision_id": decision_id, "webhook_url": webhook_url, "execution_status": "delivered"}


class _AdminOnlyDiagnosticTool(Tool):
    name = "admin_only_diagnostic_tool"
    description = "Admin only."
    inputs = {}
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self) -> dict:
        return {"ok": True}


ALL_TOOLS = [
    _GetContract(),
    _QueryData(),
    _Simulate(),
    _Recommend(),
    _PlanDecision(),
    _LogDecision(),
    _ExecuteDecision(),
    _AdminOnlyDiagnosticTool(),
]


def make_toolset(profile: str) -> AlgentaToolset:
    return AlgentaToolset(wrapped=ALL_TOOLS, profile=profile)


def test_observe_profile_exposes_exactly_the_contracts_observe_tools() -> None:
    with make_toolset("observe") as toolset:
        assert {t.name for t in toolset.tools} == {"get_contract", "query_data", "simulate", "recommend"}


def test_observe_profile_never_lists_execute_decision() -> None:
    with make_toolset("observe") as toolset:
        names = {t.name for t in toolset.tools}
        assert "execute_decision" not in names
        assert "plan_decision" not in names
        assert "log_decision" not in names


def test_govern_profile_adds_plan_and_log_but_not_execute() -> None:
    with make_toolset("govern") as toolset:
        assert {t.name for t in toolset.tools} == {
            "get_contract",
            "query_data",
            "simulate",
            "recommend",
            "plan_decision",
            "log_decision",
        }
        assert "execute_decision" not in {t.name for t in toolset.tools}


def test_execute_profile_adds_execute_decision() -> None:
    with make_toolset("execute") as toolset:
        assert {t.name for t in toolset.tools} == {
            "get_contract",
            "query_data",
            "simulate",
            "recommend",
            "plan_decision",
            "log_decision",
            "execute_decision",
        }
        assert "admin_only_diagnostic_tool" not in {t.name for t in toolset.tools}


def test_full_profile_exposes_everything_the_server_advertises() -> None:
    with make_toolset("full") as toolset:
        assert {t.name for t in toolset.tools} == {t.name for t in ALL_TOOLS}
        assert any(t.name == "admin_only_diagnostic_tool" for t in toolset.tools)


def test_unknown_profile_rejected_at_construction_time() -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        AlgentaToolset(wrapped=ALL_TOOLS, profile="admin")  # type: ignore[arg-type]


def test_default_profile_is_observe() -> None:
    with AlgentaToolset(wrapped=ALL_TOOLS) as toolset:
        assert toolset.profile == "observe"
