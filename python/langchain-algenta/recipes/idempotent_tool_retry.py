"""Idempotent tool retries: the engine's idempotency gate as your dedup mechanism.

"Retry the tool call" is where agent systems quietly double-charge customers: the first
call lands, the response is lost in transit, the retry lands *again*. This recipe shows
the retry policy that can't do that -- because the engine's `execute_decision` idempotency
gate makes duplicates structurally impossible:

- retry **only** transport-level errors (`httpx.TransportError` and subclasses -- the
  "we genuinely don't know if the call landed" class),
- if a retry comes back blocked on the `idempotency` gate, that's the engine telling you
  the original call *did* land: return `AlreadyDelivered`, do **not** reach for `force`
  (an operator-only field this package never exposes to models),
- `confidence` / `risk_floor` denials are policy decisions, not transient failures: they
  propagate immediately and are never retried.

The result: at-most-once execution semantics from the caller's side, with the engine's
receipt/denial contract as the dedup authority.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.idempotent_tool_retry
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from langchain_algenta import (
    AlgentaExecutionBlocked,
    ExecutionReceipt,
    create_algenta_tools,
    parse_receipt,
)
from langchain_core.tools import BaseTool

from ._stub import serve_recipe_stub
from ._support import first_text_json, tool_by_name


@dataclass(frozen=True)
class Delivered:
    """The call landed and returned a real receipt (possibly after transport retries)."""

    receipt: ExecutionReceipt
    attempts: int


@dataclass(frozen=True)
class AlreadyDelivered:
    """The idempotency gate says this decision was already delivered -- the original call
    landed even though its response never reached us. Not an error; do not re-execute."""

    decision_id: str
    attempts: int


IdempotentExecutionResult = Delivered | AlreadyDelivered


async def execute_idempotent(
    execute_decision: BaseTool,
    *,
    decision_id: str,
    webhook_url: str,
    max_attempts: int = 3,
) -> IdempotentExecutionResult:
    """Execute with at-most-once semantics: retry transport errors, honor the idempotency gate."""
    attempts = 0
    while True:
        attempts += 1
        try:
            raw = await execute_decision.ainvoke({"decision_id": decision_id, "webhook_url": webhook_url})
        except httpx.TransportError:
            # The only retryable class: the call may not have reached the engine at all.
            if attempts >= max_attempts:
                raise
            continue
        except AlgentaExecutionBlocked as blocked:
            if blocked.gate == "idempotency":
                return AlreadyDelivered(decision_id=decision_id, attempts=attempts)
            raise  # confidence / risk_floor: policy said no -- never retried.
        receipt = parse_receipt(first_text_json(raw))
        if receipt is None:
            raise RuntimeError(f"execute_decision returned an unrecognized payload: {raw!r}")
        return Delivered(receipt=receipt, attempts=attempts)


async def run_idempotent_tool_retry(base_url: str, *, decision_id: str, webhook_url: str) -> dict[str, object]:
    """Demo both paths: a flaky transport that recovers, and a retry after a lost response."""
    tools = await create_algenta_tools(base_url=base_url, profile="execute")
    execute_decision = tool_by_name(tools, "execute_decision")

    # Path 1: one transport failure, then success -- retried transparently.
    flaky_calls = 0

    async def _flaky(**kwargs: object) -> object:
        nonlocal flaky_calls
        flaky_calls += 1
        if flaky_calls == 1:
            raise httpx.ConnectError("connection reset by peer")
        return await execute_decision.ainvoke(kwargs)

    from langchain_core.tools import StructuredTool

    flaky_tool = StructuredTool.from_function(
        name="execute_decision",
        description=execute_decision.description,
        args_schema=execute_decision.args_schema if isinstance(execute_decision.args_schema, dict) else None,
        coroutine=_flaky,
    )
    retried = await execute_idempotent(flaky_tool, decision_id=decision_id, webhook_url=webhook_url)

    # Path 2: the first call landed (above) but pretend its response was lost -- the very
    # next attempt hits the idempotency gate and dedups instead of double-executing.
    deduped = await execute_idempotent(execute_decision, decision_id=decision_id, webhook_url=webhook_url)

    # Proof of at-most-once: the stub engine's diagnostics counter counts real deliveries.
    ops_tools = await create_algenta_tools(base_url=base_url, profile="full")
    diagnostics = first_text_json(await tool_by_name(ops_tools, "_test_diagnostics").ainvoke({}))

    return {"retried": retried, "deduped": deduped, "delivered_count": diagnostics["delivered_count"]}


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_idempotent_tool_retry(base_url, decision_id="decision-1", webhook_url="https://example.com/hook")
        print("After transport failure + retry:", result["retried"])
        print("Retry after lost response:", result["deduped"])
        print(f"Actual deliveries on the engine: {result['delivered_count']} (at-most-once holds)")


if __name__ == "__main__":
    asyncio.run(main())
