"""`create_algenta_tools` -- builds a governed-execution-aware list of LangChain `BaseTool`s from
a self-hosted Algenta Engine's MCP tool surface.

Layers three things on top of the wrapped MCP tools' real calls:

1. **Tool-profile filtering** (`profile=`): only the tool names
   [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
   assigns to the requested profile are returned. Defaults to `"observe"` (read-only), matching
   the contract's own stated default.
2. **Never-model-facing scrubbing**: `force`/`override_safety` are stripped from every returned
   tool's advertised JSON schema (this module) *and* from the arguments dict actually forwarded
   to the wrapped MCP call
   (`langchain_algenta.interceptor.AlgentaToolCallInterceptor`/`_wrap_plain_tool` below).
3. **The real `execute_decision` denial mapping** -- see
   `langchain_algenta.governance.resolve_governed_call`.

The real path (`base_url=`, the common case) builds a `MultiServerMCPClient` with
`AlgentaToolCallInterceptor` registered as a `tool_interceptor` -- per the adapter library's own
documented seam for this (see `interceptor.py`'s module docstring) -- rather than manually
re-wrapping each `StructuredTool` `get_tools()` returns. The `tools=` escape hatch (an in-memory
list of `BaseTool`s -- a fake registry in a test, or any `BaseTool`s not obtained via an MCP
client at all) has no MCP client to hang an interceptor off of, so it *does* rebuild each tool's
coroutine directly, sharing the exact same `resolve_governed_call` mapping logic.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from langchain_core.tools import BaseTool, StructuredTool

from .contract import (
    DEFAULT_PROFILE,
    NEVER_MODEL_FACING_FIELDS,
    TOOL_PROFILES,
    ToolProfile,
    resolve_profile_tool_names,
)
from .exceptions import AlgentaExecutionBlocked
from .governance import resolve_governed_call
from .interceptor import AlgentaToolCallInterceptor
from .receipts import ExecutionDenial, parse_denial

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=`
#: constructor argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"

#: The server name this package registers its own `MultiServerMCPClient` connection under, when
#: it builds the client itself (i.e. whenever `client=` isn't supplied).
_DEFAULT_SERVER_NAME = "algenta"


def _strip_never_model_facing_schema(schema: Any) -> Any:
    """Return `schema` with `NEVER_MODEL_FACING_FIELDS` removed from `properties`/`required`.

    Returns `schema` itself, unchanged, when it isn't a `dict` or none of those fields are
    present -- so callers can cheaply tell via `is` whether anything actually changed. Only
    `dict` (raw JSON Schema) is handled: every tool `langchain_mcp_adapters.get_tools()` returns
    has exactly this shape (`StructuredTool(args_schema=tool.inputSchema, ...)`, confirmed
    directly against the installed library's `tools.py`), which is the shape this package's own
    real MCP path (`base_url=`) always produces. A `tools=`-supplied `BaseTool` built with a
    Pydantic model `args_schema` instead of a raw dict is left as-is -- outside this package's
    real-world target shape, and not what any test in this suite constructs.
    """
    if not isinstance(schema, dict):
        return schema
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not (NEVER_MODEL_FACING_FIELDS & properties.keys()):
        return schema
    new_schema = dict(schema)
    new_schema["properties"] = {k: v for k, v in properties.items() if k not in NEVER_MODEL_FACING_FIELDS}
    required = schema.get("required")
    if isinstance(required, list):
        new_schema["required"] = [name for name in required if name not in NEVER_MODEL_FACING_FIELDS]
    return new_schema


def _strip_never_model_facing_tool(tool: BaseTool) -> BaseTool:
    """Defense-in-depth companion to the call-time argument scrub in `AlgentaToolCallInterceptor`
    / `_wrap_plain_tool`.

    `contracts/integration-tool-contract.json`'s `never_model_facing_note` says `force` /
    `override_safety` must never reach the model "not in a tool schema, not via a kwarg the
    model's function-call arguments can reach". The call-time scrub handles the second half;
    this handles the first -- if a misconfigured or future server ever advertised one of these
    fields on a tool's schema, the model would never see it as an available parameter to begin
    with.

    Returns `tool` itself, unchanged, when nothing needed stripping (via `_strip_never_model_facing_schema`
    returning its input `is`-identical) -- so a tool that never had these fields isn't needlessly
    rebuilt.
    """
    new_schema = _strip_never_model_facing_schema(tool.args_schema)
    if new_schema is tool.args_schema:
        return tool
    return tool.model_copy(update={"args_schema": new_schema})


def _scrub_never_model_facing_args(args: dict[str, Any]) -> dict[str, Any]:
    if not (NEVER_MODEL_FACING_FIELDS & args.keys()):
        return args
    return {k: v for k, v in args.items() if k not in NEVER_MODEL_FACING_FIELDS}


def _wrap_plain_tool(tool: BaseTool, *, denial_model: type[ExecutionDenial]) -> BaseTool:
    """Rebuild `tool` (already profile-filtered and schema-scrubbed) with a coroutine that scrubs
    call-time arguments and runs the same `resolve_governed_call` mapping the interceptor uses.

    Only used by `create_algenta_tools(tools=...)` -- the in-memory escape hatch for `BaseTool`s
    that never went through an MCP client (a fake registry in a test, or a caller's own
    pre-built tools). Calls `tool.ainvoke(...)` on the *original* tool (captured over the
    schema-scrubbed copy handed back to the model) so this works uniformly whether the original
    tool's own implementation is sync or async.
    """

    async def _run(**kwargs: Any) -> Any:
        scrubbed = _scrub_never_model_facing_args(kwargs)
        try:
            raw_result = await tool.ainvoke(scrubbed)
        except Exception as exc:
            # `tool.ainvoke(...)` in this in-memory escape hatch has no MCP
            # `CallToolResult.isError` flag to check -- a plain `BaseTool`'s coroutine reports
            # "this call was blocked" the only way a plain Python function can, by raising.
            # `parse_denial` accepts the exception's own stringified message directly and
            # recovers an embedded JSON denial body from it, tolerating surrounding prose the
            # same way it does for the real MCP `isError=True` path.
            denial = parse_denial(str(exc), model=denial_model)
            if denial is not None:
                raise AlgentaExecutionBlocked(
                    f"Algenta tool {tool.name!r} was blocked by the {denial.gate!r} policy gate -- {denial.message}",
                    denial=denial,
                ) from exc
            # Not a recognized denial shape (a generic failure, or some future error shape this
            # package doesn't know about yet) -- propagate the tool's own original exception
            # unchanged rather than inventing an Algenta-specific one for it.
            raise
        return resolve_governed_call(tool.name, raw_result, raw_result, denial_model=denial_model, is_error=False)

    scrubbed_schema = _strip_never_model_facing_schema(tool.args_schema)
    return StructuredTool.from_function(
        name=tool.name,
        description=tool.description,
        args_schema=scrubbed_schema if isinstance(scrubbed_schema, dict) else None,
        coroutine=_run,
    )


def _connection_kwargs_given(
    base_url: str | None, headers: dict[str, str] | None, auth: httpx.Auth | None, extra: dict[str, Any]
) -> bool:
    return base_url is not None or headers is not None or auth is not None or bool(extra)


async def create_algenta_tools(
    *,
    base_url: str | None = None,
    profile: ToolProfile = DEFAULT_PROFILE,
    tools: list[BaseTool] | None = None,
    client: Any | None = None,
    server_name: str = _DEFAULT_SERVER_NAME,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | None = None,
    denial_model: type[ExecutionDenial] = ExecutionDenial,
    **connection_kwargs: Any,
) -> list[BaseTool]:
    """Build a governed-execution-aware list of `BaseTool`s from a self-hosted Algenta Engine.

    Args:
        base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
            `ALGENTA_BASE_URL` environment variable, falling back to
            `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta cloud
            default). Ignored if `tools` or `client` is given.
        profile: Which tool profile to expose -- one of `"observe"` (default), `"govern"`,
            `"execute"`, or `"full"`. See
            [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
        tools: Advanced escape hatch / test seam: a pre-built list of `BaseTool`s to filter and
            wrap instead of connecting over MCP at all (e.g. an in-memory fake registry in a
            test). Mutually exclusive with `client` and with the MCP-connection arguments
            (`base_url`, `headers`, `auth`, `server_name`, `**connection_kwargs`). Since there's
            no MCP client to attach an interceptor to, this path wraps each tool's own coroutine
            directly (see `_wrap_plain_tool`) instead.
        client: Advanced escape hatch: an already-constructed
            `langchain_mcp_adapters.client.MultiServerMCPClient` to call `get_tools()` on,
            instead of having this function build one. **You are responsible for registering
            `AlgentaToolCallInterceptor(profile=profile, denial_model=denial_model)` in its own
            `tool_interceptors=` at construction** -- this function still performs
            `get_tools()`-level profile filtering and schema scrubbing regardless, but the
            call-time enforcement (the real `execute_decision` denial mapping, and the
            argument-scrub defense-in-depth layer) only happens if the interceptor is wired into
            the client itself, which this function cannot retrofit onto a client it didn't
            build. Mutually exclusive with `tools` and with the MCP-connection arguments.
        server_name: The server name to register this connection under (when this function
            builds its own client) or to call `client.get_tools(server_name=...)` with (when
            `client` is given). Defaults to `"algenta"`.
        headers: Extra HTTP headers for the self-hosted endpoint (e.g. a static bearer token).
            Forwarded to the constructed connection's `headers`. For per-call/dynamic headers
            (e.g. forwarding an end-user identity), register your own additional
            `ToolCallInterceptor` ahead of `AlgentaToolCallInterceptor` that calls
            `request.override(headers=...)` -- see
            `langchain_mcp_adapters.interceptors.MCPToolCallRequest.override`.
        auth: An `httpx.Auth` instance for the self-hosted endpoint (e.g. OAuth-style dynamic
            auth). Forwarded to the constructed connection's `auth`.
        denial_model: The `ExecutionDenial` subclass to validate a blocked `execute_decision`
            call's `{"error": {...}}` body against. Override if your engine's denial envelope
            has grown fields you want typed (the base model already accepts and preserves
            unknown fields via `extra="allow"`, so most callers won't need this).
        **connection_kwargs: Any other `langchain_mcp_adapters.sessions.StreamableHttpConnection`
            key (e.g. `timeout`, `sse_read_timeout`, `httpx_client_factory`), forwarded as-is.

    Returns:
        The tools allowed under `profile`, each with `force`/`override_safety` stripped from its
        advertised schema.

    Raises:
        ValueError: If `profile` isn't one of the four contract profiles, or if more than one of
            `tools`, `client`, and the MCP-connection arguments are given together.
    """
    if profile not in TOOL_PROFILES:
        raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")

    connection_kwargs_given = _connection_kwargs_given(base_url, headers, auth, connection_kwargs)

    if tools is not None and (client is not None or connection_kwargs_given):
        raise ValueError(
            "Pass exactly one of `tools`, `client`, or the MCP-connection arguments "
            "(`base_url`/`headers`/`auth`/**connection_kwargs), not more than one."
        )
    if client is not None and connection_kwargs_given:
        raise ValueError(
            "Pass either `client` or the MCP-connection arguments (`base_url`/`headers`/`auth`/"
            "**connection_kwargs), not both."
        )

    if tools is not None:
        source_tools = {tool.name: tool for tool in tools}
        allowed_names = resolve_profile_tool_names(profile, available_tool_names=frozenset(source_tools))
        return [
            _wrap_plain_tool(_strip_never_model_facing_tool(tool), denial_model=denial_model)
            for name, tool in source_tools.items()
            if name in allowed_names
        ]

    from langchain_mcp_adapters.client import MultiServerMCPClient

    if client is None:
        resolved_base_url = base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
        connection: dict[str, Any] = {"transport": "streamable_http", "url": resolved_base_url}
        if headers is not None:
            connection["headers"] = headers
        if auth is not None:
            connection["auth"] = auth
        connection.update(connection_kwargs)
        interceptor = AlgentaToolCallInterceptor(profile=profile, denial_model=denial_model)
        client = MultiServerMCPClient({server_name: connection}, tool_interceptors=[interceptor])

    raw_tools = await client.get_tools(server_name=server_name)
    allowed_names = resolve_profile_tool_names(profile, available_tool_names=frozenset(tool.name for tool in raw_tools))
    return [_strip_never_model_facing_tool(tool) for tool in raw_tools if tool.name in allowed_names]


__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "create_algenta_tools",
]
