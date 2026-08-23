"""Exceptions raised by `llamaindex_algenta.toolset`'s wrapped tool calls for a denied, failed,
or (in the fail-closed fallback case) still-pending governed call.

Unlike `pydantic_ai`'s own `ApprovalRequired`/`ToolDenied`/`ToolFailed`, or
`agent_framework.MiddlewareFailure` (`maf_algenta`'s foundation), llama-index has no single named
exception type for "this tool call is gated." What it has -- verified live, not assumed, against
installed `llama-index-core` 0.14.24 / `llama-index-workflows` 2.23.3 (see the package README's
"Approval mapping" section for the full reproduction) -- is a real, working pause/resume primitive
for the *pending-approval* case specifically:
`workflows.context.context.Context.wait_for_event()`, which every wrapped tool built by
`llamaindex_algenta.toolset.create_algenta_tools` calls itself when a receipt comes back
`approval_state == "pending"`. That call raises an internal `workflows.runtime.types.results.
WaitingForEvent` control-flow exception that the workflow runtime is documented to catch and turn
into a real pause -- *replaying the entire step* (here: the tool call itself, MCP round trip
included) once a human/caller resumes it, rather than resuming execution mid-function. See
`llamaindex_algenta.toolset`'s module docstring for exactly what that does and doesn't guarantee.

These three exceptions below are this package's own plain exceptions (matching every sibling
package's precedent of not inventing a fake shared base class across frameworks) for the other
three governed-execution outcomes that have no comparable real primitive to bind to:

- `AlgentaToolDenied` -- a deliberate governance denial (`approval_state` `"rejected"`/`"expired"`,
  or a named policy-gate `code`).
- `AlgentaToolExecutionFailed` -- a generic execution-level failure, including the MCP
  protocol-level `isError=True` case (Q3, layer 1: a server-side tool crash the MCP SDK already
  turned into ordinary response data, never a raised exception at any layer).
- `AlgentaApprovalStillPending` -- raised only as a fail-closed fallback when `wait_for_event()`
  itself cannot be used to pause at all: `ctx` isn't wired to a live, running workflow (a bare
  `FunctionTool.acall()` outside any `Workflow`/`FunctionAgent` run raises
  `workflows.errors.ContextStateError` the moment `wait_for_event` is called), or the wait timed
  out (`asyncio.TimeoutError`) with no human response ever arriving. Both are caught and re-raised
  as this one exception type, so a caller has one thing to catch regardless of which underlying
  reason applies.

All three propagate out of `FunctionTool.acall()` completely unmodified (verified live: no
try/except anywhere in `FunctionTool.acall`/`.call`). Whether they reach *your* code unmodified
depends entirely on what calls the tool: a bare `await tool.acall(...)` lets them through as-is;
going through `llama_index.core.tools.calling.acall_tool` or a real `FunctionAgent`/`AgentWorkflow`
run (`BaseWorkflowAgent._call_tool`) gets them caught and turned into an ordinary
`ToolOutput(is_error=True, exception=e)` / `ToolCallResult` instead -- verified live with a
deliberately-blowing-up tool (see the package README's "Exception propagation" section) -- *except*
for the one control-flow exception `wait_for_event()` itself raises internally, which is the one
exception type `BaseWorkflowAgent._call_tool` explicitly re-raises rather than swallowing. A
caller that wants `AlgentaToolDenied`/`AlgentaToolExecutionFailed`/`AlgentaApprovalStillPending` to
actually stop a `FunctionAgent.run()` must inspect the `ToolCallResult`/`ToolOutput` it produces
(`tool_output.is_error`, `tool_output.exception`), not wrap `agent.run()` in a `try`/`except` --
documented honestly here rather than papering over it, matching `haystack_algenta`'s and
`maf_algenta`'s own documented stance on their respective runtimes' default swallowing behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .receipts import GovernedExecutionReceipt


class AlgentaGovernedCallFailure(Exception):
    """Base class for the three governed-execution exceptions below.

    Carries the parsed `receipt` (when one was available) so a caller catching this can still
    inspect `error.receipt.plan_hash`, `error.receipt.execution_id`, etc.
    """

    def __init__(self, message: str, *, receipt: "GovernedExecutionReceipt | None" = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class AlgentaToolDenied(AlgentaGovernedCallFailure):
    """Raised when the engine denies a governed call outright.

    Corresponds to `GovernedExecutionReceipt.is_denied()`: `approval_state` is `"rejected"` or
    `"expired"`, or `code` is one of `NAMED_POLICY_GATE_CODES` (e.g. `"plan_hash_mismatch"`).
    """


class AlgentaToolExecutionFailed(AlgentaGovernedCallFailure):
    """Raised when a governed call did not complete successfully for a reason that is neither a
    denial nor a pending approval.

    Covers both a receipt-shaped failure (e.g. `code="upstream_timeout"`, `status="error"`, an
    `approval_state` that isn't a recognized success) and the MCP protocol-level `isError=True`
    case (`receipt` is `None` on the exception in that second case -- there was no
    governed-execution envelope to parse at all, see `llamaindex_algenta.receipts.is_call_error`).
    """


class AlgentaApprovalStillPending(AlgentaGovernedCallFailure):
    """Raised only when `Context.wait_for_event()` itself could not be used to pause.

    Two real, distinct causes collapse into this one exception type:

    1. `ctx` is not wired to a live, running workflow (`workflows.errors.ContextStateError`) --
       e.g. a bare `await tool.acall(plan_hash=..., ctx=Context(workflow))` called outside any
       `Workflow.run()`/`FunctionAgent.run()`. There is no live step to pause, so this package
       fails closed rather than silently proceeding as if the call had succeeded.
    2. The wait timed out (`asyncio.TimeoutError`) -- a real pause happened, but no human/caller
       ever sent back a `HumanResponseEvent` within the configured `approval_wait_timeout`.

    In the common case -- a real `FunctionAgent`/`AgentWorkflow` run, a human resuming within the
    timeout -- this exception is never raised at all: `wait_for_event()` pauses for real, and per
    its own documented semantics the *entire step is replayed* once resumed, which for this
    package's wrapped tools means the underlying MCP call (here: `execute_decision` or whichever
    governed tool paused) is transparently redone from scratch -- there is no separate "retry"
    code path to write, unlike `langchain_algenta`'s hand-built single-retry `resolve_governed_call`
    (LangGraph's `interrupt()` has the same replay-the-node semantics, so this mirrors that
    sibling's design, just for free). See the package README for the full accounting, including
    why this makes `execute_decision`'s caller-supplied idempotency key load-bearing here in a way
    it wouldn't be for a tool without automatic replay-on-resume.
    """


__all__ = [
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
]
