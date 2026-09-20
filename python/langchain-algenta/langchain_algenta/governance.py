"""`resolve_governed_call` -- the one piece of denial-mapping logic shared by both of this
package's two call paths (see `langchain_algenta.interceptor` for the real MCP path, and
`langchain_algenta.toolset` for the in-memory `tools=` escape hatch used by tests and by callers
without a real Algenta MCP endpoint to connect to yet).

The real `execute_decision` tool has exactly two outcomes, decided synchronously in the same
call: it succeeds (a real `ExecutionReceipt`), or it is blocked by one of exactly three named
policy gates (an `ExecutionDenial`, surfaced as an MCP tool execution error /
`CallToolResult(isError=True)`). There is no third "pending" outcome for this function to pause
on -- earlier versions of this module called `langgraph.types.interrupt(...)` here for a
fictional `approval_state == "pending"` receipt shape that the real engine never returns; that
pause (and the retry-after-resume machinery around it) has been removed outright, not replaced
with anything -- there is nothing left on this tool for a caller to resume.

Kept generic over the "raw result" type (`T`) precisely because the two call paths disagree on
what that type is: the interceptor path's raw result is an `mcp.types.CallToolResult`, returned
unchanged on success so `langchain_mcp_adapters`' own downstream content conversion still runs;
the `tools=` path's raw result is whatever plain value the wrapped tool's own coroutine returned.
"""

from __future__ import annotations

from typing import Any

from .exceptions import AlgentaExecutionBlocked
from .receipts import ExecutionDenial, parse_denial


def resolve_governed_call[T](
    tool_name: str,
    raw_result: T,
    payload: Any,
    *,
    denial_model: type[ExecutionDenial] = ExecutionDenial,
    is_error: bool = False,
) -> T:
    """Resolve one governed call's outcome, given its raw result, its already-extracted payload,
    and whether the underlying call was reported as an error.

    - `is_error` is `False` -- the call completed (a real `ExecutionReceipt`, or any other,
      unrelated tool's own result shape entirely). Returns `raw_result` unchanged in every case;
      this function's job is only to decide whether to raise, not to replace a successful result
      with a re-typed object -- see the README's "Why no typed receipt on the tool's return
      value" section for why, unlike this repository's other framework packages, this one
      doesn't do that. A caller who wants the typed receipt calls `parse_receipt` themselves.
    - `is_error` is `True` and `payload` parses as one of the three real named gates -- raises
      `AlgentaExecutionBlocked` with the engine's own gate/code/message/override_hint attached.
    - `is_error` is `True` but `payload` doesn't parse as a recognized denial (a generic
      transport failure, or some future error shape this package doesn't know about yet) --
      returns `raw_result` unchanged, deliberately *not* inventing an Algenta-specific exception
      for a shape that isn't actually one of the documented policy gates. On the real MCP path
      this lets `langchain_mcp_adapters`' own, already-correct handling of a generic
      `CallToolResult(isError=True)` take over downstream (either a raised `ToolException` or an
      error-status `ToolMessage`, depending on that library's own `handle_tool_errors` setting) --
      the native framework idiom for "this tool call failed" that this package has no reason to
      shadow when it isn't one of the three specific gates it actually understands.
    """
    if not is_error:
        return raw_result

    denial = parse_denial(payload, model=denial_model)
    if denial is not None:
        raise AlgentaExecutionBlocked(
            f"Algenta tool {tool_name!r} was blocked by the {denial.gate!r} policy gate -- {denial.message}",
            denial=denial,
        )
    return raw_result


__all__ = ["resolve_governed_call"]
