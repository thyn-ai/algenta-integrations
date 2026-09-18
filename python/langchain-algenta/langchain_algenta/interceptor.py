"""`AlgentaToolCallInterceptor` -- a `langchain_mcp_adapters.interceptors.ToolCallInterceptor`
that layers profile enforcement, never-model-facing scrubbing, and the real `execute_decision`
denial mapping onto every real MCP tool call made through a
`langchain_mcp_adapters.MultiServerMCPClient`.

Structural template: `langchain_mcp_adapters.interceptors.ToolCallInterceptor`, the adapter
library's own native seam for "wrap every tool call this client's tools make" -- deliberately
*not* a manual re-wrap of each `StructuredTool` `get_tools()` returns (see the package README's
"Why an interceptor, not a re-wrapped `StructuredTool`" section, and
`langchain_algenta.toolset.create_algenta_tools`'s docstring for the one place this package still
does the latter, for tools that never went through an MCP client at all).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from mcp.types import CallToolResult

from .contract import (
    DEFAULT_PROFILE,
    NEVER_MODEL_FACING_FIELDS,
    ToolProfile,
    is_tool_allowed_for_profile,
)
from .exceptions import AlgentaToolDenied
from .governance import resolve_governed_call
from .receipts import ExecutionDenial


def extract_call_tool_payload(call_result: CallToolResult) -> Any:
    """Unwrap the actual payload out of a raw MCP `CallToolResult` envelope.

    Prefers `structuredContent` (the modern, `outputSchema`-driven shape), falls back to parsing
    the first text content block as JSON (the shape an MCP server without a declared output
    schema uses -- which is exactly what `tests/stub_server.py`'s plain-`dict`-returning tools
    produce). When that text block isn't valid JSON on its own -- e.g. an `isError=True` result
    whose text some framework layer wrapped in its own prose around the real JSON body, verified
    directly against the installed `mcp` SDK's `mcp.server.fastmcp.tools.base.Tool.run` -- the
    raw text is returned as-is rather than `None`, so `receipts.parse_denial`'s own
    embedded-JSON recovery still gets a chance at it. Returns `None` only when there's truly no
    content to inspect. Mirrors `typescript/algenta-tools`'s `extractToolPayload` in
    `src/toolset.ts`.

    This function only ever *inspects* the result to decide whether it's a real execution receipt
    or a real execution denial -- it never replaces what actually gets returned to the framework.
    See `AlgentaToolCallInterceptor.__call__`, which always returns the original `CallToolResult`
    unchanged on the passthrough/success path, so `langchain_mcp_adapters`' own
    `isError`/`structuredContent` handling still runs downstream exactly as it would without this
    package involved at all.
    """
    if call_result.structuredContent is not None:
        return call_result.structuredContent
    for block in call_result.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


@dataclass
class AlgentaToolCallInterceptor:
    """Enforces tool-profile membership, never-model-facing scrubbing, and the real
    `execute_decision` denial mapping on every call `handler` makes.

    Registered via `MultiServerMCPClient(connections, tool_interceptors=[interceptor])` (or
    per-call via `load_mcp_tools(tool_interceptors=[interceptor])`) -- see
    `langchain_algenta.toolset.create_algenta_tools`, which wires this up for you. Not meant to
    be constructed directly by most callers.
    """

    profile: ToolProfile = DEFAULT_PROFILE
    denial_model: type[ExecutionDenial] = ExecutionDenial

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Callable[[MCPToolCallRequest], Awaitable[MCPToolCallResult]],
    ) -> MCPToolCallResult:
        """Intercept one MCP tool call.

        1. **Profile short-circuit**: if `request.name` isn't in the active profile, raises
           `AlgentaToolDenied` *without ever calling `handler`* -- a defense-in-depth backstop
           for a caller that bypassed `create_algenta_tools`'s profile-filtered tool list (e.g.
           by calling `client.get_tools()` directly). Under normal use, the model never sees a
           name outside its profile to begin with, so this should never actually fire.
        2. **Never-model-facing scrubbing**: `force`/`override_safety` are stripped from
           `request.args` (via `request.override(args=...)`) before `handler` is ever invoked --
           the call-time layer behind the schema-level scrub in
           `langchain_algenta.toolset._strip_never_model_facing_tool`.
        3. **Denial mapping**: calls `handler(request)` exactly once, extracts the payload from
           the resulting `CallToolResult`, and resolves it via
           `langchain_algenta.governance.resolve_governed_call` -- which raises
           `AlgentaExecutionBlocked` for one of the three real named policy gates when
           `raw_result.isError` is set, and otherwise returns the result unchanged. There is
           nothing to retry here: the real engine decides success-vs-blocked synchronously, in
           this one call, never a "pending, call again later" outcome.

        On the passthrough/success path, returns the raw `CallToolResult` completely unchanged
        (not a parsed receipt) -- see `resolve_governed_call`'s docstring for why.
        """
        if not is_tool_allowed_for_profile(request.name, self.profile):
            raise AlgentaToolDenied(
                f"Algenta tool {request.name!r} is not part of the {self.profile!r} profile; "
                "refusing to call it."
            )

        args = request.args or {}
        if NEVER_MODEL_FACING_FIELDS & args.keys():
            request = request.override(args={k: v for k, v in args.items() if k not in NEVER_MODEL_FACING_FIELDS})

        raw_result = await handler(request)
        if not isinstance(raw_result, CallToolResult):
            # A prior interceptor in the chain already short-circuited to a ToolMessage/Command
            # -- nothing left for this interceptor to parse as a receipt or a denial.
            return raw_result

        payload = extract_call_tool_payload(raw_result)
        return resolve_governed_call(
            request.name, raw_result, payload, denial_model=self.denial_model, is_error=raw_result.isError
        )


__all__ = ["AlgentaToolCallInterceptor", "extract_call_tool_payload"]
