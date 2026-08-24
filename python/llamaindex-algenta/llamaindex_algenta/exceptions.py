"""Exceptions raised by `llamaindex_algenta.toolset`'s wrapped `execute_decision` calls for a
real named-gate denial or a genuine execution failure.

**There is no pending/approval-pause state on the real `execute_decision` tool at all.** A call
either succeeds synchronously (a real `ExecutionReceipt`, see `llamaindex_algenta.receipts`) or is
blocked synchronously by exactly one of three named policy gates (a real HTTP 409 whose body this
package parses into `ExecutionDenial`) -- never "pending, come back later." An earlier version of
this package modeled a fictional asynchronous `approval_state` on every governed tool and mapped
a `"pending"` state onto `workflows.context.context.Context.wait_for_event()`, llama-index-workflows'
real human-in-the-loop pause primitive. That state does not exist on the real tool, so that mapping
-- and the `AlgentaApprovalStillPending` exception it raised as a fail-closed fallback -- has been
removed entirely for this tool. (`Context.wait_for_event()` itself is a real, generally useful
primitive; this package simply has no remaining reason of its own to call it.)

What remains are two plain exceptions (matching every sibling package's precedent of not
inventing a fake shared base class across frameworks) for the two real outcomes other than
success:

- `AlgentaToolDenied` -- `execute_decision` was blocked synchronously by one of the three real,
  named gates (`llamaindex_algenta.receipts.NAMED_EXECUTION_GATES`: `"idempotency"`,
  `"confidence"`, `"risk_floor"`). Carries the real `gate` name, the engine's `code`
  (`"execution_blocked_<gate>"`), and `override_hint` verbatim.
- `AlgentaToolExecutionFailed` -- a genuine execution-level failure that is *not* a named-gate
  denial: the MCP protocol-level `isError=True` case (a server-side tool crash the MCP SDK
  already turned into ordinary response data, never a raised exception at any layer), or a
  result that is neither a well-formed `ExecutionReceipt` nor a well-formed `ExecutionDenial`.

Both propagate out of `FunctionTool.acall()` completely unmodified (verified live: no try/except
anywhere in `FunctionTool.acall`/`.call`). Whether they reach *your* code unmodified depends
entirely on what calls the tool: a bare `await tool.acall(...)` lets them through as-is; going
through `llama_index.core.tools.calling.acall_tool` or a real `FunctionAgent`/`AgentWorkflow` run
(`BaseWorkflowAgent._call_tool`) gets them caught and turned into an ordinary
`ToolOutput(is_error=True, exception=e)` / `ToolCallResult` instead -- verified live with a
deliberately-blowing-up test tool (see the package README's "Exception propagation" section). A
caller that wants `AlgentaToolDenied`/`AlgentaToolExecutionFailed` to actually stop a
`FunctionAgent.run()` must inspect the `ToolCallResult`/`ToolOutput` it produces
(`tool_output.is_error`, `tool_output.exception`), not wrap `agent.run()` in a `try`/`except` --
documented honestly here rather than papering over it.
"""

from __future__ import annotations


class AlgentaGovernedCallFailure(Exception):
    """Base class for the two `execute_decision` failure exceptions below.

    `gate`/`code`/`override_hint` are populated only on `AlgentaToolDenied` (a real named-gate
    denial has all three); they are `None` on `AlgentaToolExecutionFailed`, which has no gate to
    report.
    """

    def __init__(
        self,
        message: str,
        *,
        gate: str | None = None,
        code: str | None = None,
        override_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.gate = gate
        self.code = code
        self.override_hint = override_hint


class AlgentaToolDenied(AlgentaGovernedCallFailure):
    """Raised when `execute_decision` is blocked synchronously by one of the three real, named
    policy gates: `"idempotency"` (already delivered; `force=true` overrides for one
    re-execution), `"confidence"` (below `policy.min_confidence`), or `"risk_floor"` (`risk_p5`
    below `-policy.risk_floor`) -- the latter two bypassable only via `override_safety=true`.

    `self.gate` is always one of those three exact strings, `self.code` is the engine's own
    `"execution_blocked_<gate>"`, and `self.override_hint` (when the engine sent one) is its
    verbatim guidance on how a human operator, not the model, could resolve this outside the
    model-facing call (`force`/`override_safety` are never model-facing -- see
    `llamaindex_algenta.contract.NEVER_MODEL_FACING_FIELDS`).
    """


class AlgentaToolExecutionFailed(AlgentaGovernedCallFailure):
    """Raised when `execute_decision` did not complete successfully for a reason that is not one
    of the three named gates above -- either the MCP protocol-level `isError=True` case, or a
    result that parsed as neither a well-formed `ExecutionReceipt` nor a well-formed
    `ExecutionDenial`. `self.gate` is always `None` here; there was no gate to report.
    """


__all__ = [
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
]
