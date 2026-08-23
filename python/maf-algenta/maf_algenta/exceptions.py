"""Exceptions raised by `maf_algenta.toolset` for a denied, failed, or still-pending governed call.

Unlike `pydantic-ai` (`pydantic_ai.exceptions.ApprovalRequired` / `ToolFailed` and
`pydantic_ai.tools.ToolDenied`) or `langchain-algenta`'s own plain exceptions, Microsoft Agent
Framework has exactly one primitive for "abort this run, don't let the model paper over it":
`agent_framework.MiddlewareFailure` -- "the loop's explicit fail-closed escape: it is never
converted into a tool result, ... and the exception propagates to the caller of `Agent.run`"
(quoted from the installed `agent_framework` 1.15.0's own docstring, verified live: raising it
directly from a tool body -- not just from a `FunctionMiddleware` -- propagates unmodified even
with zero middleware registered on the agent; see the package README's "Approval mapping"
section for the reproduction).

The three subclasses below all inherit `MiddlewareFailure` (so `isinstance(exc,
agent_framework.MiddlewareFailure)` is always true for anything this package raises, and nothing
here can accidentally be swallowed into a tool-error result) while still giving a caller who
wants to distinguish "denied" from "failed" from "still pending after the human already
approved" something more specific to catch than the single framework primitive -- the same three-
way split `pydantic-ai-algenta` and `langchain-algenta` expose, rebuilt on MAF's actual fail-
closed mechanism instead of a bespoke one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework import MiddlewareFailure

if TYPE_CHECKING:
    from .receipts import GovernedExecutionReceipt


class AlgentaGovernedCallFailure(MiddlewareFailure):
    """Base class for a fail-closed abort raised while resolving a governed-execution receipt.

    Carries the parsed `receipt` (when one was available) so a caller who does catch this --
    typically via a `try`/`except` around `agent.run(...)`, since `MiddlewareFailure` propagates
    all the way out -- can still inspect `error.receipt.plan_hash`, `error.receipt.execution_id`,
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
    """Raised when `execute_decision`'s receipt still reports `approval_state == "pending"`.

    By construction, this can only fire *after* MAF's own `approval_mode="always_require"`
    pre-call gate has already let the call through (a human/caller approved the model's *request*
    to call `execute_decision` -- see `maf_algenta.toolset`'s module docstring for exactly how
    that gate is wired). It means the connected engine's own out-of-band policy approval for this
    call's `plan_hash` was never recorded, or hasn't propagated yet -- a different, orthogonal
    concern from MAF's pre-call gate. There is no MAF-native resumable pause to fall back to here
    (unlike `langchain-algenta`'s `langgraph.types.interrupt()`), so this is a one-shot,
    fail-closed abort: record the real approval against `receipt.plan_hash` out of band, then
    retry the whole call (a fresh model turn / a fresh `agent.run()`), rather than expecting any
    kind of resume.
    """


__all__ = [
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
]
