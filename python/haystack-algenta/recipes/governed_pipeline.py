"""Haystack Pipeline recipe: observe-profile recommendation + typed `execute_decision` denial hook.

`haystack-algenta` exposes `execute_decision` denials through an `Agent` `after_tool` hook
(`build_algenta_governance_hooks()`). A Haystack `Pipeline` has no hook seam, so this recipe shows
the equivalent: a custom Pipeline component that invokes `execute_decision` and applies the same
typed outcome parsing (`extract_execution_outcome_from_tool_result`) the hook uses. A denial still
surfaces as `AlgentaToolDenied`, just raised from inside the component instead of from a hook.

The pipeline combines:

- the read-only `observe` profile (`recommend`) for pre-execution scoring, and
- the `execute` profile's `execute_decision` tool, guarded by the typed denial hook.

Run it (from `algenta-integrations/python/haystack-algenta`):

    uv run python -m recipes.governed_pipeline
"""

from __future__ import annotations

from typing import Any

from haystack import Pipeline
from haystack.core.component import component
from haystack.core.errors import PipelineRuntimeError
from haystack.tools import Tool
from haystack_algenta import (
    AlgentaToolDenied,
    AlgentaToolset,
    ExecutionBlocked,
    ExecutionReceipt,
    create_algenta_tools,
    extract_execution_outcome_from_tool_result,
    unwrap_mcp_tool_result,
)


@component
class RecommendAction:
    """Pipeline component that calls the `observe` profile's `recommend` tool.

    This is the read-only half of the pipeline: it asks the engine for a recommended action and
    confidence score, but cannot plan, log, or execute any decision.
    """

    def __init__(self, recommend_tool: Tool) -> None:
        self._tool = recommend_tool

    @component.output_types(recommendation=dict[str, Any])
    def run(self, scenario: str) -> dict[str, Any]:
        raw = self._tool.invoke(scenario=scenario)
        return {"recommendation": unwrap_mcp_tool_result(raw)}


@component
class GovernedExecuteDecision:
    """Pipeline component that invokes `execute_decision` and raises `AlgentaToolDenied` on a real
    synchronous policy denial.

    This is the Pipeline equivalent of `haystack_algenta.hooks.GovernedReceiptHook`: it parses the
    tool result through `extract_execution_outcome_from_tool_result` and, when the outcome is an
    `ExecutionBlocked`, raises the same typed `AlgentaToolDenied` the Agent hook does.
    """

    def __init__(self, execute_decision_tool: Tool) -> None:
        self._tool = execute_decision_tool

    @component.output_types(receipt=ExecutionReceipt)
    def run(self, decision_id: str, webhook_url: str) -> dict[str, Any]:
        raw = self._tool.invoke(decision_id=decision_id, webhook_url=webhook_url)
        outcome = extract_execution_outcome_from_tool_result(raw)

        if isinstance(outcome, ExecutionBlocked):
            hint = f" ({outcome.override_hint})" if outcome.override_hint else ""
            raise AlgentaToolDenied(
                f"Algenta tool 'execute_decision' was blocked by the real {outcome.gate!r} "
                f"policy gate ({outcome.code}): {outcome.message}{hint}",
                blocked=outcome,
            )

        if not isinstance(outcome, ExecutionReceipt):
            raise RuntimeError(f"execute_decision returned an unrecognized payload: {raw!r}")

        return {"receipt": outcome}


def build_governed_pipeline(base_url: str) -> tuple[Pipeline, AlgentaToolset, AlgentaToolset]:
    """Build a Pipeline that uses the `observe` profile plus the typed `execute_decision` denial hook.

    The observe-profile toolset supplies `recommend`; a separate execute-profile toolset supplies
    `execute_decision`, wrapped here in `GovernedExecuteDecision` so denials surface as typed
    `AlgentaToolDenied` exceptions out of `pipeline.run()`.

    Args:
        base_url: The self-hosted Algenta MCP endpoint to connect to.

    Returns:
        A tuple of `(pipeline, observe_toolset, execute_toolset)`. The caller is responsible for
        calling `.close()` on both toolsets to tear down the underlying MCP connections.
    """
    observe_toolset = create_algenta_tools(base_url=base_url, profile="observe")
    execute_toolset = create_algenta_tools(base_url=base_url, profile="execute")

    recommend_tool = next(tool for tool in observe_toolset if tool.name == "recommend")
    execute_decision_tool = next(
        tool for tool in execute_toolset if tool.name == "execute_decision"
    )

    pipeline = Pipeline()
    pipeline.add_component("recommend", RecommendAction(recommend_tool))
    pipeline.add_component("execute_decision", GovernedExecuteDecision(execute_decision_tool))

    return pipeline, observe_toolset, execute_toolset


def run_governed_pipeline(
    pipeline: Pipeline, *, scenario: str, decision_id: str, webhook_url: str
) -> dict[str, Any]:
    """Run the governed pipeline and return both the recommendation and the execution receipt.

    Haystack wraps any exception raised inside a component in `PipelineRuntimeError`. This helper
    unwraps the original `AlgentaToolDenied` so callers see the same typed exception the Agent hook
    surfaces.

    Raises:
        AlgentaToolDenied: If `execute_decision` is blocked by a real policy gate.
    """
    try:
        return pipeline.run(
            {
                "recommend": {"scenario": scenario},
                "execute_decision": {"decision_id": decision_id, "webhook_url": webhook_url},
            }
        )
    except PipelineRuntimeError as exc:
        cause = exc.__cause__
        if isinstance(cause, AlgentaToolDenied):
            raise cause from None
        raise


def main() -> None:
    from tests.stub_server import (
        CONFIDENCE_BLOCKED_DECISION_ID,
        RISK_FLOOR_BLOCKED_DECISION_ID,
        StubServerFixture,
    )

    with StubServerFixture() as stub:
        print(f"Stub Algenta MCP server listening at {stub.base_url}")
        pipeline, observe_toolset, execute_toolset = build_governed_pipeline(base_url=stub.base_url)
        try:
            # Happy path: a fresh decision executes and returns a typed receipt.
            result = run_governed_pipeline(
                pipeline,
                scenario="scenario-alpha",
                decision_id="decision-pipeline-success",
                webhook_url="https://example.test/hook",
            )
            receipt = result["execute_decision"]["receipt"]
            recommendation = result["recommend"]["recommendation"]
            print(
                f"recommend: {recommendation['recommended_action']} "
                f"(confidence {recommendation['confidence']})"
            )
            print(f"execute_decision: delivered (decision_id={receipt.decision_id})")

            # Denial paths: each named gate surfaces as AlgentaToolDenied with the gate attached.
            for decision_id in (CONFIDENCE_BLOCKED_DECISION_ID, RISK_FLOOR_BLOCKED_DECISION_ID):
                try:
                    run_governed_pipeline(
                        pipeline,
                        scenario="scenario-denied",
                        decision_id=decision_id,
                        webhook_url="https://example.test/hook",
                    )
                except AlgentaToolDenied as denied:
                    print(f"execute_decision denied on the {denied.gate!r} gate: {denied}")

            # Idempotency gate: a repeat call to the same decision id is blocked.
            try:
                run_governed_pipeline(
                    pipeline,
                    scenario="scenario-alpha",
                    decision_id="decision-pipeline-success",
                    webhook_url="https://example.test/hook",
                )
            except AlgentaToolDenied as denied:
                print(f"execute_decision denied on the {denied.gate!r} gate: {denied}")
        finally:
            observe_toolset.close()
            execute_toolset.close()


if __name__ == "__main__":
    main()
