"""Exceptions raised by `haystack_algenta.hooks.GovernedReceiptHook` for a denied, failed, or
still-pending governed call.

Unlike `agent_framework.MiddlewareFailure` (`maf_algenta`'s foundation) or `pydantic_ai`'s own
`ApprovalRequired`/`ToolDenied`/`ToolFailed`, Haystack has no single named "this must abort the
run, don't let the model paper over it" primitive. What it does have -- verified live, not assumed
(see the package README's "Approval mapping" section) -- is a specific, load-bearing fact about
its `Agent` run loop: an exception raised from an `after_tool` hook propagates out of `agent.run()`
completely unmodified, because `Agent._run_step`'s call to `_run_hooks(self.hooks, AFTER_TOOL,
...)` has no surrounding `try`/`except` anywhere in `agent.py` (grep-verified against the installed
package). That is a structurally real guarantee, just not one Haystack names with its own
exception base class the way MAF does -- so these three plain exceptions are this package's own,
not a wrapper around some existing Haystack type.

Contrast this with what happens if an exception is instead raised from inside a wrapped `Tool`'s
own `function`/`async_function` (the seam every other sibling in this repository actually uses for
its approval/denial mapping): `Tool.invoke()`/`invoke_async()` unconditionally catches `Exception`
and re-raises `haystack.tools.errors.ToolInvocationError` instead (losing the original exception's
type, though not its message -- recoverable via `.__cause__`), and going through a real `Agent`
with its documented default `raise_on_tool_invocation_failure=False`, that `ToolInvocationError` is
never even re-raised -- it is swallowed into an ordinary error-flagged tool-result `ChatMessage`
fed back into the conversation, and `agent.run()` returns normally. That is precisely why this
package's receipt mapping lives in an `after_tool` hook (`haystack_algenta.hooks`) instead of
inside `haystack_algenta.toolset`'s wrapped tool calls -- see that module's docstring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .receipts import GovernedExecutionReceipt


class AlgentaGovernedCallFailure(Exception):
    """Base class for a fail-closed abort raised by `GovernedReceiptHook` while resolving a
    governed-execution receipt.

    Carries the parsed `receipt` (when one was available) so a caller catching this around
    `agent.run(...)` can still inspect `error.receipt.plan_hash`, `error.receipt.execution_id`,
    etc.
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
    """Raised when a governed call's receipt is neither a success, a denial, nor a pending
    approval -- a generic execution-level failure (e.g. `code="upstream_timeout"`)."""


class AlgentaApprovalStillPending(AlgentaGovernedCallFailure):
    """Raised when a governed call's receipt still reports `approval_state == "pending"`.

    For `execute_decision` specifically, this can fire *after* Haystack's own `ConfirmationHook`
    pre-call gate (see `haystack_algenta.hooks.default_confirmation_hook`) has already let the
    call through -- a human/caller approved the model's *request* to call `execute_decision`. That
    says nothing about whether the connected engine's own out-of-band policy approval for this
    call's `plan_hash` has actually been recorded; the two are orthogonal (see the package
    README). There is no Haystack-native resumable pause to fall back to here (unlike
    `langchain-algenta`'s `langgraph.types.interrupt()`), so this is a one-shot, fail-closed abort:
    record the real approval against `receipt.plan_hash` out of band, then retry the whole call
    (a fresh `agent.run()`), rather than expecting any kind of resume.
    """


__all__ = [
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
]
