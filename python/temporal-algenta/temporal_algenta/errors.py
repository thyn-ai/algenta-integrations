"""Exceptions raised inside this package's client/activity edge, and the mapping from a
structured policy denial onto Temporal's own failure vocabulary.

Two layers, deliberately separate:

1. **Client-side exceptions** (`AlgentaGovernedCallError` and subclasses) -- raised by
   `temporal_algenta.client.AlgentaMcpClient` inside the calling activity. They are ordinary
   Python exceptions carrying the parsed `ExecutionDenial`, easy to unit-test without a
   Temporal runtime.
2. **The Temporal failure mapping** (`denial_application_error`) -- an activity converts a
   caught `AlgentaExecutionBlocked` into a `temporalio.exceptions.ApplicationError` with:

   - `type=denial.code` (e.g. `"execution_blocked_confidence"`) -- the engine's own denial code
     becomes the Temporal error type, so `RetryPolicy(non_retryable_error_types=[...])` and
     workflow-side `except` logic can key off it;
   - `details=(denial dict,)` -- the full `{"code", "gate", "message", "override_hint"}` body,
     recoverable in workflow code via `temporal_algenta.types.denial_from_activity_error`;
   - `non_retryable=True` -- a policy denial is deterministic: the same call with the same
     arguments under the same policy can only deny again, so Temporal must not burn retries on
     it. Changing the outcome requires *different input* (an operator's `force` /
     `override_safety`, or a different decision), which is a new activity call -- see
     `recipes/human_approval_workflow.py`.

This is the durable-execution analogue of `langchain_algenta.exceptions.AlgentaExecutionBlocked`:
where a LangChain graph catches a plain exception, a Temporal workflow catches an
`ActivityError` whose cause is this typed `ApplicationError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from temporalio.exceptions import ApplicationError

from .types import DenialDetails

if TYPE_CHECKING:
    from .receipts import ExecutionDenial


class AlgentaGovernedCallError(Exception):
    """Base class for an error raised while resolving a governed call against a self-hosted
    Algenta Engine.

    Carries the parsed `denial` (when one was available) so a caller who catches this can still
    inspect `error.denial.gate`, `error.denial.code`, `error.denial.override_hint`, etc.
    """

    def __init__(self, message: str, *, denial: ExecutionDenial | None = None) -> None:
        super().__init__(message)
        self.denial = denial


class AlgentaToolDenied(AlgentaGovernedCallError):
    """Raised by `AlgentaMcpClient.call_tool`, with `denial=None`, when a tool name is called
    outside the active tool profile -- defense-in-depth against workflow/activity code wired to
    a profile that doesn't include the tool. Unrelated to anything the connected engine itself
    ever reports."""


class AlgentaExecutionBlocked(AlgentaGovernedCallError):
    """Raised when the engine blocks an `execute_decision` call synchronously -- its `409`
    response, in the very same MCP call, never a separate "pending" round trip -- on exactly
    one of three real, named policy gates:

    - `"idempotency"`: this `decision_id` was already delivered; bypassable only via
      `force=true`, and only for one re-execution.
    - `"confidence"`: below `policy.min_confidence`; bypassable only via `override_safety=true`.
    - `"risk_floor"`: `risk_p5` below `-policy.risk_floor`; bypassable only via
      `override_safety=true`.

    `.denial` carries the parsed `ExecutionDenial` (`gate`, `code`, `message`,
    `override_hint`); `.gate` is a convenience shortcut onto `denial.gate`.
    """

    @property
    def gate(self) -> str | None:
        return self.denial.gate if self.denial is not None else None


class AlgentaToolCallFailed(AlgentaGovernedCallError):
    """Raised when an MCP tool call returns `isError=True` but its payload is *not* one of the
    three named policy gates (a generic tool-execution failure, or a future error shape this
    package doesn't recognize yet). Carries the raw payload on `.payload` for diagnostics.
    Mapped by the activity layer to a *retryable* `ApplicationError` (see
    `temporal_algenta.types.TOOL_ERROR_TYPE`) -- unlike a policy denial, an unclassified tool
    error may be transient.
    """

    def __init__(self, message: str, *, payload: object = None) -> None:
        super().__init__(message, denial=None)
        self.payload = payload


def denial_application_error(denial: ExecutionDenial) -> ApplicationError:
    """Build the `ApplicationError` an activity raises for a real policy denial.

    `type` is the engine's own denial code; the denial body goes into `details` as a plain
    JSON dict (see `temporal_algenta.types.DenialDetails.to_json_dict`) so workflow code can
    recover it with `denial_from_activity_error`; `non_retryable=True` because a policy denial
    is deterministic under replay-identical input.
    """
    details = DenialDetails(
        code=denial.code, gate=denial.gate, message=denial.message, override_hint=denial.override_hint
    )
    return ApplicationError(
        f"Algenta governed execution blocked by the {denial.gate!r} policy gate -- {denial.message}",
        details.to_json_dict(),
        type=denial.code,
        non_retryable=True,
    )


__all__ = [
    "AlgentaExecutionBlocked",
    "AlgentaGovernedCallError",
    "AlgentaToolCallFailed",
    "AlgentaToolDenied",
    "denial_application_error",
]
