"""Exceptions raised by `AlgentaToolCallInterceptor` (and the `tools=` escape hatch in
`langchain_algenta.toolset`) for a denied or policy-blocked governed call.

Unlike `pydantic-ai` (which has `pydantic_ai.exceptions.ToolFailed` and
`pydantic_ai.tools.ToolDenied` as first-class primitives), `langchain_core.tools` has no
denied-vs-failed distinction of its own. Rather than inventing a fake framework primitive, this
module defines plain exceptions -- deliberately *not* `langchain_core.tools.ToolException`
subclasses, so they propagate to the caller unmodified rather than being silently swallowed into
an error-status `ToolMessage` by `BaseTool`'s own `handle_tool_error` machinery (only
`ToolException` is eligible for that; see the package README's "Why plain exceptions, not
`ToolException`" section for the full reasoning, verified against `langchain_core.tools.base`).

This surfaces a real, synchronous `execute_decision` denial as a normal LangChain tool-call error
the enclosing graph can catch with a plain `try`/`except` around `ainvoke(...)` -- there is no
"pending approval" state on this tool for anything to pause on (a genuinely separate,
plan_hash+nonce human-approval system does exist on the real engine, but its own source says
explicitly that it is intentionally not exposed as an MCP/LLM tool, so no MCP-based integration
package can ever observe or wait on it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .receipts import ExecutionDenial


class AlgentaGovernedCallError(Exception):
    """Base class for an error raised while resolving a governed `execute_decision` call.

    Carries the parsed `denial` (when one was available) so a caller who does catch this can
    still inspect `error.denial.gate`, `error.denial.code`, `error.denial.override_hint`, etc.
    """

    def __init__(self, message: str, *, denial: ExecutionDenial | None = None) -> None:
        super().__init__(message)
        self.denial = denial


class AlgentaToolDenied(AlgentaGovernedCallError):
    """Raised by `AlgentaToolCallInterceptor` itself, with `denial=None`, when a tool name is
    called outside the active tool profile -- defense-in-depth against a caller that bypassed
    the profile-filtered tool list (e.g. by calling `client.get_tools()` directly on a client
    this package built). Unrelated to anything the connected engine itself ever reports.
    """


class AlgentaExecutionBlocked(AlgentaGovernedCallError):
    """Raised when the real engine blocks an `execute_decision` call synchronously -- its `409`
    response, in the very same call, never a separate "pending" round trip -- on exactly one of
    three real, named policy gates:

    - `"idempotency"`: this `decision_id` was already delivered; bypassable only via
      `force=true`, and only for one re-execution.
    - `"confidence"`: below `policy.min_confidence`; bypassable only via `override_safety=true`.
    - `"risk_floor"`: `risk_p5` below `-policy.risk_floor`; bypassable only via
      `override_safety=true`.

    `.denial` carries the parsed `ExecutionDenial` (`gate`, `code`, `message`, `override_hint`);
    `.gate` is a convenience shortcut onto `denial.gate`.
    """

    @property
    def gate(self) -> str | None:
        return self.denial.gate if self.denial is not None else None


__all__ = [
    "AlgentaExecutionBlocked",
    "AlgentaGovernedCallError",
    "AlgentaToolDenied",
]
