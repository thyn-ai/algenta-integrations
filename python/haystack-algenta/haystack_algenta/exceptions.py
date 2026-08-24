"""`AlgentaToolDenied` -- raised by `haystack_algenta.hooks.GovernedReceiptHook` when the real
engine's `execute_decision` MCP tool comes back with its real, synchronous 409 denial.

**Corrected against the real engine contract.** An earlier version of this module additionally
defined `AlgentaApprovalStillPending` (for a receipt's `approval_state == "pending"`) and
`AlgentaToolExecutionFailed` (a catch-all for "the receipt exists but isn't recognized as a
success, denial, or pending approval"). Neither has a real counterpart: `execute_decision` either
returns a 200 `ExecutionReceipt` or a 409 naming exactly one of three real gates in that *same*
call -- there is no asynchronous "pending" state to poll or resume, and no third outcome besides
"succeeded" and "blocked by a named gate" for this tool. Both exceptions are removed rather than
kept dormant for a case that cannot occur.

What's still real and unchanged, verified live against installed `haystack-ai` 3.0.0 (see the
package README's "Approval mapping" section): an exception raised from an `after_tool` hook
propagates out of `agent.run()` completely unmodified, because `Agent._run_step`'s call to
`_run_hooks(self.hooks, AFTER_TOOL, ...)` has no surrounding `try`/`except` anywhere in `agent.py`
(grep-verified against the installed package). That is a structurally real guarantee Haystack
doesn't name with its own exception base class the way `agent_framework.MiddlewareFailure` does --
so `AlgentaToolDenied` is this package's own, not a wrapper around some existing Haystack type.

Contrast this with what happens if an exception is instead raised from inside a wrapped `Tool`'s
own `function`/`async_function` (the seam every other sibling in this repository actually uses for
its approval/denial mapping): `Tool.invoke()`/`invoke_async()` unconditionally catches `Exception`
and re-raises `haystack.tools.errors.ToolInvocationError` instead (losing the original exception's
type, though not its message -- recoverable via `.__cause__`), and going through a real `Agent`
with its documented default `raise_on_tool_invocation_failure=False`, that `ToolInvocationError` is
never even re-raised -- it is swallowed into an ordinary error-flagged tool-result `ChatMessage`
fed back into the conversation, and `agent.run()` returns normally. That is precisely why this
package's outcome mapping lives in an `after_tool` hook (`haystack_algenta.hooks`) instead of
inside `haystack_algenta.toolset`'s wrapped tool calls -- see that module's docstring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .receipts import ExecutionBlocked, ExecutionGate


class AlgentaGovernedCallFailure(Exception):
    """Base class for a fail-closed abort raised by `GovernedReceiptHook` while resolving an
    `execute_decision` outcome.

    Carries the parsed `blocked` payload (when one was available) so a caller catching this
    around `agent.run(...)` can still inspect `error.gate`, `error.blocked.code`,
    `error.blocked.override_hint`, etc.
    """

    def __init__(self, message: str, *, blocked: "ExecutionBlocked | None" = None) -> None:
        super().__init__(message)
        self.blocked = blocked

    @property
    def gate(self) -> "ExecutionGate | None":
        """The real named gate (`"idempotency"` / `"confidence"` / `"risk_floor"`) that blocked
        this call, or `None` if this failure wasn't constructed with a parsed `blocked` payload."""
        return self.blocked.gate if self.blocked is not None else None

    @property
    def override_hint(self) -> str | None:
        return self.blocked.override_hint if self.blocked is not None else None


class AlgentaToolDenied(AlgentaGovernedCallFailure):
    """Raised when a real `execute_decision` call comes back with its synchronous 409, naming
    exactly one of the three real gates (see `haystack_algenta.receipts.ExecutionGate`):

    - `"idempotency"`: this `decision_id` was already delivered; bypassable only via a
      caller-supplied `force=True` for one re-execution.
    - `"confidence"`: below `policy.min_confidence`; bypassable only via `override_safety=True`.
    - `"risk_floor"`: `risk_p5` below `-policy.risk_floor`; bypassable only via
      `override_safety=True`.

    Both `force` and `override_safety` are operator/break-glass fields this package strips from
    every model-facing schema and call-time argument dict (see
    `haystack_algenta.contract.NEVER_MODEL_FACING_FIELDS`) -- from the model's perspective, this
    exception is the *only* way a denial is ever visible; there is nothing for it to retry with a
    different flag itself.
    """


__all__ = [
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
]
