"""`resolve_governed_call` -- the one piece of approval-mapping logic shared by both of this
package's two call paths (see `langchain_algenta.interceptor` for the real MCP path, and
`langchain_algenta.toolset` for the in-memory `tools=` escape hatch used by tests and by callers
without a real Algenta MCP endpoint to connect to yet).

Kept generic over the "raw result" type (`T`) precisely because those two call paths disagree on
what that type is: the interceptor path's raw result is an `mcp.types.CallToolResult` that must
be returned unchanged on success (so `langchain_mcp_adapters`' own `isError`/`structuredContent`
handling still runs downstream); the `tools=` path's raw result is just whatever plain dict the
wrapped tool's own coroutine returned. Both paths already have the *payload* (a plain dict or
`None`) extracted before calling in here -- this module only cares about the payload for parsing,
and about `raw_result` only as the value to hand back unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from langgraph.types import interrupt

from .exceptions import AlgentaApprovalStillPending, AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import GovernedExecutionReceipt, parse_receipt

T = TypeVar("T")


def _interrupt_payload(tool_name: str, receipt: GovernedExecutionReceipt) -> dict[str, Any]:
    """The value handed to `langgraph.types.interrupt(...)` for a pending governed call.

    Carries everything a human (or a policy-engine node reading `__interrupt__` off the graph
    state) needs to go record the real, out-of-band approval against `plan_hash` on the actual
    engine -- mirrors the metadata `pydantic_ai_algenta.toolset.AlgentaToolset._approval_metadata`
    attaches to `ApprovalRequired`.
    """
    return {
        "reason": "algenta_pending_approval",
        "tool_name": tool_name,
        "plan_hash": receipt.plan_hash,
        "execution_id": receipt.execution_id,
        "idempotency_key": receipt.idempotency_key,
        "policy_snapshot_hash": receipt.policy_snapshot_hash,
        "receipt": receipt.model_dump(),
    }


async def resolve_governed_call(
    tool_name: str,
    raw_result: T,
    payload: Any,
    *,
    receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt,
    retry: Callable[[], Awaitable[tuple[T, Any]]] | None,
    _retried: bool = False,
) -> T:
    """Resolve one governed call's outcome, given its raw result and already-extracted payload.

    - `payload` doesn't parse as a `GovernedExecutionReceipt` at all (e.g. `get_contract`'s
      discovery payload) -- returns `raw_result` unchanged.
    - `receipt.is_pending_approval()`, and this is the first time this call has seen a pending
      result (`_retried=False`, always true for a caller's own top-level call) -- calls
      `langgraph.types.interrupt(...)` with the receipt's identifying fields (see the package
      README's "Why `interrupt()`, and what happens after resume" section for exactly what this
      does and doesn't guarantee). Once resumed, if `retry` is given, calls it *once* to redo the
      exact same underlying call -- now that a human/policy engine has (hopefully) recorded the
      real approval against `plan_hash` on the engine side -- and resolves whatever that retry
      returns through this same function, with `_retried=True`. If no `retry` is given (the
      caller has no way to redo the call), raises `AlgentaApprovalStillPending` immediately after
      the resume, without ever attempting a second call.
    - `receipt.is_pending_approval()` a *second* time (`_retried=True`: the one retry above has
      already happened and the engine still hasn't recorded the approval) -- raises
      `AlgentaApprovalStillPending` directly, deliberately *without* calling `interrupt()` again.
      `langgraph.types.interrupt()` is resumable per call site within a task, not idempotent
      across calls -- pausing a second time here would mean a resumer who approved once and
      resumed would just get paused forever on every not-yet-approved retry instead of ever
      seeing a clear failure, which defeats the point of the single-retry design.
    - `receipt.is_denied()` -- raises `AlgentaToolDenied` with the engine's own denial reason.
    - Anything else that isn't `receipt.is_success()` -- raises `AlgentaToolExecutionFailed`.
    - A successful receipt -- returns `raw_result` unchanged (the caller already has it; this
      function's job is only to decide whether to raise, pause, or pass through, not to replace
      the result with a re-typed object -- see the README's "Why no typed receipt on the tool's
      return value" section for why, unlike the pydantic-ai and TypeScript siblings, this package
      doesn't do that).
    """
    receipt = parse_receipt(payload, model=receipt_model)
    if receipt is None:
        return raw_result

    if receipt.is_pending_approval():
        if _retried:
            raise AlgentaApprovalStillPending(
                f"Algenta tool {tool_name!r} is still pending server-side policy approval "
                f"(plan_hash={receipt.plan_hash!r}) after one retry following the resume -- "
                "the out-of-band approval doesn't appear to have been recorded against this "
                "plan_hash yet.",
                receipt=receipt,
            )
        interrupt(_interrupt_payload(tool_name, receipt))
        # Execution resumes here (via `Command(resume=...)`) once a human/graph caller decides
        # to continue. `interrupt()`'s own return value (whatever the resumer supplied) isn't
        # used for anything here -- the resumer's real signal is "go check again", not a value
        # this function needs to interpret.
        if retry is None:
            raise AlgentaApprovalStillPending(
                f"Algenta tool {tool_name!r} is still pending server-side policy approval "
                f"(plan_hash={receipt.plan_hash!r}) and no retry path was available to check "
                "again after resume.",
                receipt=receipt,
            )
        new_raw_result, new_payload = await retry()
        return await resolve_governed_call(
            tool_name, new_raw_result, new_payload, receipt_model=receipt_model, retry=retry, _retried=True
        )

    if receipt.is_denied():
        raise AlgentaToolDenied(f"Algenta tool {tool_name!r} was denied by policy -- {receipt.denial_reason()}", receipt=receipt)

    if not receipt.is_success():
        raise AlgentaToolExecutionFailed(
            f"{receipt.code}: Algenta tool {tool_name!r} did not complete successfully "
            f"(status={receipt.status!r}).",
            receipt=receipt,
        )

    return raw_result


__all__ = ["resolve_governed_call"]
