"""Exceptions raised by `AlgentaToolCallInterceptor` for a denied or failed governed call.

Unlike `pydantic-ai` (which has `pydantic_ai.exceptions.ToolFailed` and
`pydantic_ai.tools.ToolDenied` as first-class primitives), `langchain_core.tools` has no
denied-vs-failed distinction of its own. Rather than inventing a fake framework primitive, this
module defines two plain exceptions -- deliberately *not* `langchain_core.tools.ToolException`
subclasses, so they propagate to the caller unmodified rather than being silently swallowed into
an error-status `ToolMessage` by `BaseTool`'s own `handle_tool_error` machinery (only
`ToolException` is eligible for that; see the package README's "Why plain exceptions, not
`ToolException`" section for the full reasoning, verified against `langchain_core.tools.base`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .receipts import GovernedExecutionReceipt


class AlgentaGovernedCallError(Exception):
    """Base class for an error raised while resolving a governed-execution receipt.

    Carries the parsed `receipt` (when one was available) so a caller who does catch this can
    still inspect `error.receipt.plan_hash`, `error.receipt.execution_id`, etc.
    """

    def __init__(self, message: str, *, receipt: "GovernedExecutionReceipt | None" = None) -> None:
        super().__init__(message)
        self.receipt = receipt


class AlgentaToolDenied(AlgentaGovernedCallError):
    """Raised when the engine denies a governed call outright.

    Corresponds to `GovernedExecutionReceipt.is_denied()`: `approval_state` is `"rejected"` or
    `"expired"`, or `code` is one of `NAMED_POLICY_GATE_CODES` (e.g. `"plan_hash_mismatch"`).
    Also raised by `AlgentaToolCallInterceptor` itself, with `receipt=None`, when a tool name is
    called outside the active tool profile (defense-in-depth against a caller that bypassed the
    profile-filtered tool list).
    """


class AlgentaToolExecutionFailed(AlgentaGovernedCallError):
    """Raised when a governed call's receipt is neither a success, a denial, nor a pending
    approval -- a generic execution-level failure (e.g. `code="upstream_timeout"`)."""


class AlgentaApprovalStillPending(AlgentaGovernedCallError):
    """Raised when a governed call is still `approval_state == "pending"` after the one retry
    `AlgentaToolCallInterceptor` performs following `langgraph.types.interrupt(...)`.

    This is the "no checkpointer, or the resume didn't actually get the plan approved in time"
    case -- see the package README's "What happens after resume" section. It is *not* raised for
    the first pending observation; that one goes through `interrupt()` instead (see
    `langchain_algenta.interceptor`).
    """


__all__ = [
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallError",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
]
