"""`create_algenta_tools` -- a governed-execution-aware `list[FunctionTool]` wrapping a
self-hosted Algenta Engine's MCP tool surface, built on `llama-index-tools-mcp`'s own real
`BasicMCPClient` (a real, separate PyPI package -- confirmed directly from its installed
`.dist-info/METADATA` that its only dependencies are `llama-index-core`, `mcp`, and `pydantic`;
the full `llama-index` metapackage is never required). Per the plan's own "LlamaIndex consumes MCP
in workflows" framing, this package's job is Algenta's governance value-add layered on top of
LlamaIndex's real, existing MCP support -- not a from-scratch MCP client.

**Why this module does not simply call `McpToolSpec(client=...).to_tool_list_async()` and wrap the
result**, unlike how naturally that would read: verified live against installed
`llama-index-tools-mcp` 0.4.8 that `McpToolSpec._create_tool_fn`'s closure is
`async def async_tool_fn(**kwargs): return await self.client.call_tool(tool_name, kwargs)` --
no `ctx: Context` parameter anywhere. `FunctionTool.__init__` detects whether a wrapped function
needs workflow context purely by inspecting its signature for a `Context`-annotated parameter
(`requires_context` / `ctx_param_name`), so every tool `McpToolSpec` builds has
`requires_context=False` and **cannot itself call `ctx.wait_for_event()`** -- the one real,
verified-live human-in-the-loop primitive this package needs for a paused `execute_decision` (see
below). This module therefore builds its own `async def wrapper(ctx: Context, **kwargs)` per
allowed tool and hands that to `FunctionTool.from_defaults(...)` directly, reusing only
`McpToolSpec`'s public JSON-Schema-to-pydantic-model helpers (`create_model_from_json_schema`,
`remove_model_fields`) rather than reinventing that conversion.

Layers three things on top of each already-profile-allowed real MCP tool call:

1. **Tool-profile filtering** (`profile=`): only the tool names
   [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
   assigns to the requested profile are ever turned into a `FunctionTool` at all. Unlike
   Haystack's `MCPToolset(tool_names=...)`, the raw MCP protocol's `list_tools()` has no
   server-side name filter to layer this on top of, so this is a single client-side filter over
   whatever the connected engine's `list_tools()` actually returns -- fully adequate on its own
   (the model is never even shown a disallowed tool's schema), just not "two layers" the way the
   Haystack sibling's convenience wrapper happens to offer.
2. **Two-layer never-model-facing scrubbing**: `force`/`override_safety` are stripped from every
   wrapped tool's advertised pydantic `fn_schema` (via `McpToolSpec.remove_model_fields`, the same
   real utility `partial_params_by_tool` uses internally -- verified live that this is
   schema-only and bypassable by an explicit kwarg, exactly like every sibling's own finding) *and*
   from the arguments dict actually forwarded to `client.call_tool(...)`, so a model (or caller)
   that still supplies one of these fields explicitly cannot get it through either layer.
3. **The governed-execution receipt mapping** (approval/denial/failure -- see
   `llamaindex_algenta.exceptions` for the full accounting of what happens to each outcome once it
   leaves this module, including the real `Context.wait_for_event()` pause for a pending approval).

**Why this module does not need any connection lifecycle (`connect()`/`close()`)**, unlike every
other sibling package's toolset module: verified live from `BasicMCPClient`'s real source
(`llama_index/tools/mcp/client.py`) that every public method (`list_tools`, `call_tool`, ...) opens
and tears down its own MCP session independently via `_run_session()`'s `async with` -- there is no
persistent connection this module builds and would need to own the lifecycle of. A caller who
wants connection reuse/pooling can pass their own pre-built `client=` (see `create_algenta_tools`).
"""

import asyncio
import os
from typing import Any

# Deliberately NO `from __future__ import annotations` in this module, unlike every sibling in
# this repository: `FunctionTool.__init__` detects whether a wrapped callable needs workflow
# context by inspecting the *raw* `inspect.signature(...)` annotation of its `ctx`/`Context`
# parameter (`_is_context_param`, in `llama_index/core/tools/function_tool.py`) -- it does not
# resolve postponed/string annotations (no `typing.get_type_hints()` anywhere in that check).
# Verified live: with the future import enabled, `_build_tool`'s `wrapper(ctx: Context, ...)`
# signature carries the *string* `"Context"` as its annotation instead of the real class object,
# `_is_context_param` returns `False` for it, `FunctionTool.requires_context` ends up `False`,
# and every call through a real `FunctionAgent`/`_call_tool` then fails with a plain Python
# `TypeError: wrapper() missing 1 required positional argument: 'ctx'` (caught and swallowed into
# an ordinary `ToolOutput(is_error=True, ...)`, so this would have been a silent, hard-to-diagnose
# breakage rather than an import error) -- caught by this package's own test suite, not by
# inspection. Every other type annotation in this module still uses modern `X | None`/`list[X]`
# syntax directly (safe at runtime on the `>=3.10` this package requires).

from llama_index.core.tools import FunctionTool
from llama_index.core.tools.types import ToolMetadata
from llama_index.core.workflow import Context, HumanResponseEvent, InputRequiredEvent
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec
from workflows.errors import ContextStateError

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile, resolve_profile_tool_names
from .exceptions import AlgentaApprovalStillPending, AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import GovernedExecutionReceipt, call_error_text, is_call_error, parse_receipt, unwrap_call_tool_result

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=` argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"

#: Default `timeout=` passed to `Context.wait_for_event()` for a pending governed approval, in
#: seconds. Matches `Context.wait_for_event`'s own documented default (2000s) -- overridable per
#: call via `create_algenta_tools(approval_wait_timeout=...)`.
DEFAULT_APPROVAL_WAIT_TIMEOUT: float = 2000.0


def _scrub_never_model_facing_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    if not (NEVER_MODEL_FACING_FIELDS & kwargs.keys()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k not in NEVER_MODEL_FACING_FIELDS}


async def _resolve_governed_call(
    *,
    ctx: Context,
    tool_name: str,
    raw: Any,
    receipt_model: type[GovernedExecutionReceipt],
    approval_wait_timeout: float | None,
) -> Any:
    """Turn one raw `client.call_tool(...)` result into whatever this tool call should return to
    the model, or raise one of `llamaindex_algenta.exceptions`'s three exceptions.

    See that module's docstring for the full accounting of what happens to each raised exception
    depending on what actually calls this wrapped tool.
    """
    payload = unwrap_call_tool_result(raw)
    receipt = parse_receipt(payload, model=receipt_model)

    if receipt is None:
        if is_call_error(raw):
            # MCP protocol-level failure (Q3, layer 1): the connected server's own tool
            # implementation raised, and the MCP SDK already turned that into ordinary
            # (non-raising) response data. Treat it as a failure -- never silently pass it
            # through as if it were an ordinary, non-governed success.
            raise AlgentaToolExecutionFailed(
                f"Algenta tool {tool_name!r} failed at the MCP protocol level: {call_error_text(raw)}"
            )
        # Not a governed-execution envelope at all (e.g. get_contract's discovery payload) --
        # pass it through unchanged, exactly as the contract's own wording requires.
        return payload if payload is not None else raw

    if receipt.is_pending_approval():
        waiter_id = f"algenta:{tool_name}:{receipt.plan_hash or receipt.execution_id or 'unknown'}"
        try:
            await ctx.wait_for_event(
                HumanResponseEvent,
                waiter_id=waiter_id,
                waiter_event=InputRequiredEvent(
                    tool_name=tool_name,
                    plan_hash=receipt.plan_hash,
                    execution_id=receipt.execution_id,
                    msg=(
                        f"Algenta: {tool_name!r} for plan_hash={receipt.plan_hash!r} is pending "
                        "out-of-band policy approval. Approve?"
                    ),
                ),
                requirements={"plan_hash": receipt.plan_hash} if receipt.plan_hash else None,
                timeout=approval_wait_timeout,
            )
        except (ContextStateError, asyncio.TimeoutError) as exc:
            # `ContextStateError`: `ctx` isn't wired to a live, running workflow (e.g. a bare
            # `await tool.acall(...)` outside any `Workflow`/`FunctionAgent` run) -- there is no
            # live step to pause, so this fails closed rather than proceeding as if approved.
            # `asyncio.TimeoutError`: a real pause happened but nobody resumed within
            # `approval_wait_timeout`.
            raise AlgentaApprovalStillPending(
                f"Algenta tool {tool_name!r} is still pending server-side policy approval "
                f"(plan_hash={receipt.plan_hash!r}): {exc}",
                receipt=receipt,
            ) from exc
        # Reached only if some future/alternate runtime resumes execution in-place instead of
        # replaying the whole step from the top, per `Context.wait_for_event`'s own documented
        # semantics ("replays the entire step when the event arrives") -- the real workflows
        # runtime redoes this tool call (MCP round trip included) from scratch on resume, so in
        # practice this line is never reached on the original attempt. Kept as a fail-closed
        # guard rather than silently returning a stale, still-pending result.
        raise AlgentaApprovalStillPending(  # pragma: no cover -- unreachable under the real runtime
            f"Algenta tool {tool_name!r} resumed without its step replaying (unexpected runtime "
            "behavior) -- the pending approval was never actually re-checked.",
            receipt=receipt,
        )

    if receipt.is_denied():
        raise AlgentaToolDenied(
            f"Algenta tool {tool_name!r} was denied by policy -- {receipt.denial_reason()}", receipt=receipt
        )

    if not receipt.is_success():
        raise AlgentaToolExecutionFailed(
            f"{receipt.code}: Algenta tool {tool_name!r} did not complete successfully "
            f"(status={receipt.status!r}).",
            receipt=receipt,
        )

    return receipt


def _build_tool(
    *,
    client: Any,
    raw_tool: Any,
    schema_builder: McpToolSpec,
    receipt_model: type[GovernedExecutionReceipt],
    approval_wait_timeout: float | None,
) -> FunctionTool:
    """Build one governed-execution-aware `FunctionTool` for `raw_tool` (an `mcp.types.Tool`).

    `schema_builder` is a throwaway `McpToolSpec` instance used only for its public
    `create_model_from_json_schema`/`remove_model_fields` helpers -- never for
    `fetch_tools`/`to_tool_list_async`, whose own tool-call closures this module deliberately does
    not use (see this module's docstring).
    """
    tool_name = raw_tool.name
    model_schema = schema_builder.create_model_from_json_schema(raw_tool.inputSchema or {}, model_name=f"{tool_name}_Schema")

    never_facing_present = NEVER_MODEL_FACING_FIELDS & set(model_schema.model_fields)
    if never_facing_present:
        model_schema = schema_builder.remove_model_fields(
            model_schema, never_facing_present, model_name=f"{tool_name}_Schema"
        )

    async def wrapper(ctx: Context, **kwargs: Any) -> Any:
        raw = await client.call_tool(tool_name, _scrub_never_model_facing_args(kwargs))
        return await _resolve_governed_call(
            ctx=ctx,
            tool_name=tool_name,
            raw=raw,
            receipt_model=receipt_model,
            approval_wait_timeout=approval_wait_timeout,
        )

    metadata = ToolMetadata(name=tool_name, description=raw_tool.description or "", fn_schema=model_schema)
    return FunctionTool.from_defaults(async_fn=wrapper, tool_metadata=metadata)


async def create_algenta_tools(
    *,
    base_url: str | None = None,
    profile: ToolProfile = DEFAULT_PROFILE,
    client: Any | None = None,
    receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt,
    approval_wait_timeout: float | None = DEFAULT_APPROVAL_WAIT_TIMEOUT,
    **client_kwargs: Any,
) -> list[FunctionTool]:
    """Build the governed-execution-aware `FunctionTool` list for a self-hosted Algenta Engine.

    ```python
    from llama_index.core.agent.workflow import FunctionAgent
    from llamaindex_algenta import create_algenta_tools

    tools = await create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")
    agent = FunctionAgent(tools=tools, llm=...)
    result = await agent.run(user_msg="what should we do?")
    ```

    Args:
        base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
            `ALGENTA_BASE_URL` environment variable, falling back to
            `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta cloud
            default). Ignored if `client` is given.
        profile: Which tool profile to expose to the model -- one of `"observe"` (default),
            `"govern"`, `"execute"`, or `"full"`. See
            [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
        client: Advanced escape hatch / test seam: an already-constructed MCP client to use
            instead of having this function build a `BasicMCPClient` from `base_url`. Must
            support the same duck-typed async surface `McpToolSpec` itself requires:
            `list_tools()` and `call_tool(name, arguments)`. Mutually exclusive with `base_url`
            and `**client_kwargs`. **You own this value's own lifecycle** (this function only
            ever calls `list_tools()`/`call_tool()` on it).
        receipt_model: The `GovernedExecutionReceipt` subclass to validate tool results against.
        approval_wait_timeout: Forwarded as `timeout=` to `Context.wait_for_event()` when a call
            comes back `approval_state == "pending"`. `None` means wait indefinitely.
        **client_kwargs: Any other `BasicMCPClient` constructor keyword argument (e.g. `headers`,
            `timeout`, `auth`), forwarded as-is when this function builds its own client. Ignored
            (and rejected, see below) if `client` is given.

    Returns:
        One `FunctionTool` per tool name `profile` allows that the connected engine's `list_tools()`
        actually advertises right now, each with `force`/`override_safety` stripped from its
        advertised schema and scrubbed from its call-time arguments, and each carrying the
        governed-execution receipt mapping described in this module's docstring.

    Raises:
        ValueError: If `profile` isn't one of the four contract profiles, or if both `client` and
            any MCP-connection argument (`base_url`/`**client_kwargs`) are given together.
    """
    if profile not in TOOL_PROFILES:
        raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")

    connection_kwargs_given = base_url is not None or bool(client_kwargs)
    if client is not None and connection_kwargs_given:
        raise ValueError(
            "Pass either `client` (an already-built MCP client) or the MCP-connection arguments "
            "(`base_url`/**client_kwargs), not both."
        )

    if client is None:
        resolved_base_url = base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
        client = BasicMCPClient(resolved_base_url, **client_kwargs)

    list_result = await client.list_tools()
    raw_tools = list(getattr(list_result, "tools", list_result) or [])
    available_names = frozenset(t.name for t in raw_tools)
    allowed_names = resolve_profile_tool_names(profile, available_tool_names=available_names)

    schema_builder = McpToolSpec(client=client)
    return [
        _build_tool(
            client=client,
            raw_tool=raw_tool,
            schema_builder=schema_builder,
            receipt_model=receipt_model,
            approval_wait_timeout=approval_wait_timeout,
        )
        for raw_tool in raw_tools
        if raw_tool.name in allowed_names
    ]


__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_APPROVAL_WAIT_TIMEOUT",
    "create_algenta_tools",
]
