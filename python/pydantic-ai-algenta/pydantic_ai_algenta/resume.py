"""`approve_and_resume` -- a thin convenience over the real deferred-tool-approval primitives.

The full round trip for a governed execution paused for approval is, using only pydantic-ai's
own primitives (see `AlgentaToolset.call_tool`):

1. `result = await agent.run(...)` ends early with `result.output` being a
   [`DeferredToolRequests`][pydantic_ai.tools.DeferredToolRequests] (because
   `AlgentaToolset.call_tool` raised `ApprovalRequired`).
2. The caller inspects `result.output.metadata[tool_call_id]` (`plan_hash`, `execution_id`,
   `idempotency_key`, ...) and calls *the engine's own real approval endpoint*, via their own
   `algenta-sdk` client, to actually approve the plan.
3. The caller builds a
   [`DeferredToolResults`][pydantic_ai.tools.DeferredToolResults] with
   `results.approvals[tool_call_id] = True` and resumes:
   `await agent.run(message_history=result.all_messages(), deferred_tool_results=results)`.

`approve_and_resume` only collapses steps 2 and 3 into one call -- it does not hardcode step 2's
engine call. See the "Why `approve` is a callback, not a hardcoded SDK call" note in the package
README: the published `algenta-sdk` (checked directly against PyPI 1.0.11 while building this
package, since it's the one thing this package is allowed to depend on) does not expose a
`decision_plans.approve(execution_id)`-shaped method matching this envelope's `plan_hash` /
`execution_id` keys -- its nearest real analog is the differently-shaped, run-id-keyed
`approve_agent_run(run_id)`. Hardcoding a call to an endpoint that doesn't exist on the real,
published client would violate this package's one hard rule (depend only on what's actually
published), so `approve_and_resume` takes your approval call as a callback instead of assuming
its shape -- and will pick up a better-matching SDK method transparently the moment one exists,
with no change to this function.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

ApproveCallback = Callable[[dict[str, Any]], "Awaitable[Any] | Any"]
"""Called once per pending approval in a `DeferredToolRequests`, with that call's
`ApprovalRequired` metadata (`plan_hash`, `execution_id`, `idempotency_key`, `receipt`, ...).
Do the real approval call here -- e.g. `lambda metadata: client.approve_agent_run(...)`, shaped
to however your engine's approval endpoint actually looks. May be sync or async; a coroutine
return value is awaited. Raising propagates out of `approve_and_resume` and the run is not
resumed."""


async def approve_and_resume(
    agent: Any,
    *,
    message_history: list[ModelMessage],
    deferred_requests: DeferredToolRequests,
    approve: ApproveCallback,
    **run_kwargs: Any,
) -> AgentRunResult[Any]:
    """Approve every pending approval in `deferred_requests` and resume `agent`'s run.

    For each `ToolCallPart` in `deferred_requests.approvals`, calls `approve(metadata)` with the
    metadata `AlgentaToolset.call_tool` attached when it raised `ApprovalRequired`. Once every
    `approve(...)` call has returned (or been awaited) without raising, builds a
    [`DeferredToolResults`][pydantic_ai.tools.DeferredToolResults] approving every one of those
    tool calls and resumes `agent.run(message_history=..., deferred_tool_results=...)`.

    Args:
        agent: The `Agent` (or any object with a matching async `run(...)` method) whose paused
            run produced `deferred_requests`.
        message_history: The paused run's message history (`result.all_messages()`).
        deferred_requests: The paused run's output (`result.output`, a `DeferredToolRequests`).
        approve: Called once per pending approval; see `ApproveCallback`.
        **run_kwargs: Forwarded to `agent.run(...)` (e.g. `usage_limits`, `model`, `deps`).

    Returns:
        The resumed run's `AgentRunResult`. If the engine still reports the plan pending after
        `approve()` returns (e.g. the approval hasn't propagated yet), this may itself be another
        `DeferredToolRequests` -- `AlgentaToolset.call_tool` raises `ApprovalRequired` again in
        that case, and pydantic-ai surfaces it the same way as the first pause.

    Note:
        This only resolves `deferred_requests.approvals` (human-in-the-loop approvals raised via
        `ApprovalRequired`, which is all `AlgentaToolset.call_tool` ever raises).
        `deferred_requests.calls` (external-execution deferrals raised via `CallDeferred`, which
        no code in this package raises) are left untouched; a caller mixing `AlgentaToolset`
        with tools of their own that use `CallDeferred` should build their own
        `DeferredToolResults` covering both instead of using this helper.
    """
    results = DeferredToolResults()
    for call in deferred_requests.approvals:
        metadata = deferred_requests.metadata.get(call.tool_call_id, {})
        outcome = approve(metadata)
        if inspect.isawaitable(outcome):
            await outcome
        results.approvals[call.tool_call_id] = True

    return await agent.run(message_history=message_history, deferred_tool_results=results, **run_kwargs)


__all__ = ["approve_and_resume", "ApproveCallback"]
