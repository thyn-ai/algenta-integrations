"""Structured-denial error handling: policy denials as first-class control flow.

LangChain's tool-error idiom (`handle_tool_error`) is built for "tell the model it failed
so it can self-correct". An Algenta policy denial is not that: it's a *decision* -- the
engine saying no, with a named reason, in the same call. `langchain-algenta` deliberately
raises `AlgentaExecutionBlocked` as a plain exception (never swallowed into an error-status
`ToolMessage`), and this recipe shows the production pattern for handling it: map every
`execute_decision` outcome onto a small typed result and branch on it explicitly.

Three outcome shapes, matching the engine's real contract:

- `Delivered` -- the webhook delivery landed (`execution_status="delivered"`).
- `DeliveryFailed` -- the call completed and returned a real receipt, but the webhook
  target rejected delivery (`execution_status="failed"`). A delivery problem, not a policy
  decision: alerting/requeueing territory.
- `PolicyDenied` -- blocked on exactly one of three named gates, with the parsed
  `ExecutionDenial` (`gate`, `code`, `message`, `override_hint`) attached:
    - `idempotency`: this decision was already delivered -- a duplicate call, not a
      failure; safe to treat as "already done".
    - `confidence` / `risk_floor`: policy said no. Do not silently retry these; surface
      them. Only a human operator's `override_safety` (outside the model-facing tool call
      entirely) can bypass them.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.structured_denial_handling
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from langchain_algenta import (
    AlgentaExecutionBlocked,
    ExecutionDenial,
    ExecutionReceipt,
    create_algenta_tools,
    parse_receipt,
)
from langchain_core.tools import BaseTool

from ._stub import serve_recipe_stub
from ._support import first_text_json, tool_by_name


@dataclass(frozen=True)
class Delivered:
    receipt: ExecutionReceipt


@dataclass(frozen=True)
class DeliveryFailed:
    receipt: ExecutionReceipt


@dataclass(frozen=True)
class PolicyDenied:
    denial: ExecutionDenial


ExecutionOutcome = Delivered | DeliveryFailed | PolicyDenied


async def execute_with_outcome(
    execute_decision: BaseTool, *, decision_id: str, webhook_url: str
) -> ExecutionOutcome:
    """Call `execute_decision` once and map its real contract onto a typed outcome."""
    try:
        raw = await execute_decision.ainvoke({"decision_id": decision_id, "webhook_url": webhook_url})
    except AlgentaExecutionBlocked as blocked:
        if blocked.denial is None:
            raise
        return PolicyDenied(blocked.denial)
    receipt = parse_receipt(first_text_json(raw))
    if receipt is None:
        raise RuntimeError(f"execute_decision returned an unrecognized payload: {raw!r}")
    return Delivered(receipt) if receipt.is_delivered() else DeliveryFailed(receipt)


def describe(outcome: ExecutionOutcome) -> str:
    """The one-line operator summary of an outcome -- what a runbook would print."""
    match outcome:
        case Delivered(receipt):
            return f"delivered to {receipt.webhook_url} (HTTP {receipt.response_code})"
        case DeliveryFailed(receipt):
            return f"delivery failed (HTTP {receipt.response_code}) -- alert the webhook owner"
        case PolicyDenied(denial) if denial.gate == "idempotency":
            return "already delivered -- the idempotency gate deduped this call; nothing to do"
        case PolicyDenied(denial):
            return f"policy denied on the {denial.gate!r} gate: {denial.message} (not retryable)"


async def run_structured_denial_handling(base_url: str) -> dict[str, ExecutionOutcome]:
    """Walk all four real outcome paths against the stub engine; return each typed outcome."""
    from tests.stub_server import FAILED_DELIVERY_DECISION_ID, LOW_CONFIDENCE_DECISION_ID

    tools = await create_algenta_tools(base_url=base_url, profile="execute")
    execute_decision = tool_by_name(tools, "execute_decision")
    webhook_url = "https://example.com/hook"

    delivered = await execute_with_outcome(execute_decision, decision_id="decision-1", webhook_url=webhook_url)
    failed_delivery = await execute_with_outcome(
        execute_decision, decision_id=FAILED_DELIVERY_DECISION_ID, webhook_url=webhook_url
    )
    # `decision-1` was delivered above; the repeat call exercises the idempotency gate.
    duplicate = await execute_with_outcome(execute_decision, decision_id="decision-1", webhook_url=webhook_url)
    low_confidence = await execute_with_outcome(
        execute_decision, decision_id=LOW_CONFIDENCE_DECISION_ID, webhook_url=webhook_url
    )

    return {
        "delivered": delivered,
        "failed_delivery": failed_delivery,
        "duplicate": duplicate,
        "low_confidence": low_confidence,
    }


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        outcomes = await run_structured_denial_handling(base_url)
        for label, outcome in outcomes.items():
            print(f"{label:>15}: {describe(outcome)}")


if __name__ == "__main__":
    asyncio.run(main())
