"""Exceptions raised by `maf_algenta.toolset` for a denied or anomalous `execute_decision` call.

Unlike `pydantic-ai` (`pydantic_ai.tools.ToolDenied`) or `langchain-algenta`'s own plain
exceptions, Microsoft Agent Framework has exactly one primitive for "abort this run, don't let
the model paper over it": `agent_framework.MiddlewareFailure` -- "the loop's explicit fail-closed
escape: it is never converted into a tool result, ... and the exception propagates to the caller
of `Agent.run`" (quoted from the installed `agent_framework` 1.15.0's own docstring, verified
live: raising it directly from a tool body -- not just from a `FunctionMiddleware` -- propagates
unmodified even with zero middleware registered on the agent).

Both classes below inherit `MiddlewareFailure` (so `isinstance(exc,
agent_framework.MiddlewareFailure)` is always true for anything this package raises, and neither
can accidentally be swallowed into a tool-error result the model then sees and might paper over)
while still giving a caller who wants to distinguish "the real engine deliberately blocked this"
from "something else went wrong" something more specific to catch than the single framework
primitive.

There is no third, "still pending" exception here (an earlier version of this module had one,
`AlgentaApprovalStillPending`, modeled on an async human-approval state that does not exist
anywhere on the real `execute_decision` tool -- see `maf_algenta.receipts` for the full
accounting). `execute_decision` either succeeds synchronously or is blocked synchronously, in the
very same call; there is nothing to be "still pending" about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework import MiddlewareFailure

if TYPE_CHECKING:
    from .receipts import ExecutionBlocked


class AlgentaGovernedCallFailure(MiddlewareFailure):
    """Base class for a fail-closed abort raised while resolving a real `execute_decision` call.

    Carries the parsed `blocked` detail (when the failure is a real, recognized policy-gate
    denial -- see `AlgentaToolDenied`) so a caller who does catch this -- typically via a
    `try`/`except` around `agent.run(...)`, since `MiddlewareFailure` propagates all the way out
    -- can still inspect `error.blocked.gate`, `error.blocked.override_hint`, etc.
    """

    def __init__(self, message: str, *, blocked: ExecutionBlocked | None = None) -> None:
        super().__init__(message)
        self.blocked = blocked


class AlgentaToolDenied(AlgentaGovernedCallFailure):
    """Raised when the real engine blocks `execute_decision` synchronously (its real 409),
    carrying one of the three real named gates -- `"idempotency"`, `"confidence"`, or
    `"risk_floor"` -- in `self.blocked.gate` (see `maf_algenta.receipts.ExecutionBlocked`).

    `force=true` bypasses only the `"idempotency"` gate, for one re-execution;
    `override_safety=true` bypasses only `"confidence"`/`"risk_floor"`. Neither field is ever
    model-facing (see `maf_algenta.contract.NEVER_MODEL_FACING_FIELDS`) -- resolving a denial
    means a human operator decides whether to retry the call with one of them set, out of band.
    """


class AlgentaToolExecutionFailed(AlgentaGovernedCallFailure):
    """Raised when a real `execute_decision` call completed without error (no 409) but its
    payload does not validate as the real `ExecutionReceipt` shape.

    This is a genuine anomaly, not a documented outcome -- the real contract says a non-error
    `execute_decision` result is always a real receipt. `self.blocked` is always `None` here.
    """


__all__ = [
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
]
