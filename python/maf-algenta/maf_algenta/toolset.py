"""`create_algenta_tools` -- an async context manager yielding a governed-execution-aware list of
`agent_framework.FunctionTool`s from a self-hosted Algenta engine's MCP tool surface.

Structural template: `agent_framework.MCPStreamableHTTPTool` itself, used the same way MAF's own
research examples and docs use it -- `async with MCPStreamableHTTPTool(name=..., url=...) as
mcp_tool:` -- since one `MCPStreamableHTTPTool` instance *is* a live, connected client session;
there is no separate "MultiServerMCPClient"-shaped abstraction to attach an interceptor to, the
way `langchain-mcp-adapters` has one. `create_algenta_tools` mirrors that exact lifecycle: it is
itself an async context manager (built with `contextlib.asynccontextmanager`) that owns a fresh
`MCPStreamableHTTPTool` connection for the duration of the `async with` block and yields the
governed, wrapped tool list.

Layers three things on top of each wrapped MCP tool's real calls:

1. **Tool-profile filtering** (`profile=`): only the tool names
   [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
   assigns to the requested profile are exposed to the model -- enforced twice: once at
   connection time via `MCPStreamableHTTPTool(allowed_tools=...)` (so the server-advertised
   function list this package even sees is already filtered), and again as a defense-in-depth
   pass over whatever `.functions` actually comes back (`resolve_profile_tool_names`), which is
   what the `mcp_tool=` escape hatch below (an already-connected tool this function did not
   build itself) relies on for its own filtering, since `allowed_tools` can only be set at
   *construction* time.
2. **Two-layer never-model-facing scrubbing**: `force`/`override_safety` are stripped from every
   wrapped tool's advertised schema (the `input_model` handed to the new `FunctionTool`) *and*
   from the arguments dict actually forwarded to the real underlying MCP call. Both layers matter
   independently here, more so than in this repository's other Python siblings: MAF's own
   argument validator (`agent_framework._tools._validate_arguments_against_schema`, verified by
   reading the installed 1.15.0 source directly) only rejects an *unexpected* argument when the
   tool's schema explicitly sets `"additionalProperties": false` -- which real MCP-derived
   schemas do not set by default. That means a schema-level scrub alone is cosmetic: a model that
   still emitted `force` as an argument (e.g. copied from an earlier turn) would sail straight
   through schema validation and reach the real call unless the call-time scrub below also
   strips it. This was verified directly, not assumed -- see the package README's "Why two
   layers, verified" section for the reproduction proving a smuggled `force=True` never reaches
   the underlying tool.
3. **`execute_decision`'s real denial mapping** (`_governed_execute_decision_call`, inside
   `_wrap_mcp_function`): `execute_decision` -- and only `execute_decision`, since it is the only
   tool in this repository's real contract with a documented receipt/denial shape -- either
   succeeds (a real `maf_algenta.receipts.ExecutionReceipt`, passed through to the model
   unchanged) or is blocked synchronously by the real engine (an MCP tool-error whose text is the
   real `{"error": {"code": "execution_blocked_<gate>", ...}}` 409 body). The latter is parsed
   and re-raised as `maf_algenta.exceptions.AlgentaToolDenied`, one of the two
   `agent_framework.MiddlewareFailure` subclasses in `maf_algenta.exceptions` -- see that module's
   docstring, and the package README's "Denial mapping" section, for the full reasoning. Every
   other tool this package wraps (`get_contract`, `query_data`, `simulate`, `recommend`,
   `plan_decision`, `log_decision`) has no verified receipt/denial contract of its own, so this
   package imposes none on them: their raw results pass straight through, scrubbed but otherwise
   untouched.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from agent_framework import FunctionTool, MCPStreamableHTTPTool
from agent_framework.exceptions import ToolExecutionException

from .contract import (
    DEFAULT_PROFILE,
    EXECUTE_DECISION,
    NEVER_MODEL_FACING_FIELDS,
    TOOL_PROFILES,
    ToolProfile,
    resolve_profile_tool_names,
)
from .exceptions import AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import (
    ExecutionBlocked,
    ExecutionReceipt,
    parse_execution_blocked,
    parse_execution_receipt,
)

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=`
#: constructor argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"

#: The name this package registers its own `MCPStreamableHTTPTool` connection under, when it
#: builds one itself (i.e. whenever `mcp_tool=` isn't supplied).
_DEFAULT_SERVER_NAME = "algenta"


@runtime_checkable
class _ConnectedMCPTool(Protocol):
    """The minimal shape `create_algenta_tools(mcp_tool=...)` needs from an escape-hatch value:
    an already-connected `agent_framework.MCPTool` (or a fake standing in for one in a test) with
    a `.functions` list. Not `agent_framework.MCPTool` itself, so a test can pass a plain object
    exposing `.functions` without needing a real MCP connection at all -- see
    `tests/test_profile_filtering.py` / `tests/test_never_model_facing.py`, which do exactly
    that, reserving the real network round trip for `tests/test_toolset_scenarios.py`.
    """

    @property
    def functions(self) -> list[FunctionTool]: ...


def _strip_never_model_facing_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return `schema` with `NEVER_MODEL_FACING_FIELDS` removed from `properties`/`required`.

    Returns `schema` itself, unchanged, when none of those fields are present -- so callers can
    cheaply tell via `is` whether anything actually changed.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not (NEVER_MODEL_FACING_FIELDS & properties.keys()):
        return schema
    new_schema = dict(schema)
    new_schema["properties"] = {k: v for k, v in properties.items() if k not in NEVER_MODEL_FACING_FIELDS}
    required = schema.get("required")
    if isinstance(required, list):
        new_schema["required"] = [name for name in required if name not in NEVER_MODEL_FACING_FIELDS]
    return new_schema


def _scrub_never_model_facing_args(args: dict[str, Any]) -> dict[str, Any]:
    if not (NEVER_MODEL_FACING_FIELDS & args.keys()):
        return args
    return {k: v for k, v in args.items() if k not in NEVER_MODEL_FACING_FIELDS}


def _extract_function_result_payload(contents: Any) -> Any:
    """Unwrap the actual JSON payload out of `FunctionTool.invoke(..., skip_parsing=True)`'s
    real return value for an MCP-derived function -- verified live to be a `list[Content]` whose
    first item is `Content(type="text", text=<json>)` (the stub server's plain-dict tool returns
    have no declared MCP output schema, exactly like `tests/stub_server.py`'s siblings in
    `pydantic-ai-algenta`/`langchain-algenta`). Also accepts a bare dict (the `mcp_tool=` escape
    hatch's fake-registry tests build plain async functions that just return a dict directly,
    with no `Content` wrapping at all) and, defensively, a `Content` item exposing a structured
    `.result` dict directly, mirroring `langchain_algenta.interceptor.extract_call_tool_payload`'s
    `structuredContent`-first preference. Returns `None` when nothing recognizable is found.
    """
    if isinstance(contents, dict):
        return contents
    if not isinstance(contents, list):
        return None
    for item in contents:
        result = getattr(item, "result", None)
        if isinstance(result, dict):
            return result
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return None


def _parse_execution_blocked_from_exception(exc: ToolExecutionException) -> ExecutionBlocked | None:
    """Try to recover the real `{"error": {...}}` 409 body out of a `ToolExecutionException`.

    `agent_framework`'s own MCP client raises this exception with the underlying MCP
    `CallToolResult`'s joined text content as its message whenever `isError` comes back true
    (verified directly against the installed `agent-framework-core` 1.15.0 source -- see this
    module's docstring). A real `execute_decision` 409 denial reaches this package exactly that
    way: `tests/stub_server.py`'s `execute_decision` raises a plain exception carrying the real
    JSON-encoded error body as its message, and the real MCP server framework wraps *any*
    exception raised inside a tool function as `ToolError(f"Error executing tool {name}: {e}")`
    (confirmed directly against the installed `mcp` SDK's
    `mcp.server.fastmcp.tools.base.Tool.run` source) before turning it into that `isError=True`
    text -- so the JSON body arrives with a human-readable prefix in front of it, not on its own.
    This looks for the first `{` and parses from there, rather than assuming the whole text is
    JSON, to survive that prefix (and similar ones a different real MCP server framework might
    use) without depending on its exact wording. Returns `None` when no such JSON object is found,
    or it doesn't parse as the real denial shape -- the signal `_governed_execute_decision_call`
    uses to tell a real, recognized policy-gate denial apart from some other, unrelated failure.
    """
    text = str(exc)
    start = text.find("{")
    if start == -1:
        return None
    try:
        body = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return parse_execution_blocked(body)


async def _governed_execute_decision_call(
    real_fn: FunctionTool, *, receipt_model: type[ExecutionReceipt], **kwargs: Any
) -> Any:
    """The real `execute_decision` call: a real `ExecutionReceipt` (success, returned unchanged)
    or a real, named policy-gate denial (raised as `AlgentaToolDenied`) -- never anything else,
    per the real contract (see this module's and `maf_algenta.receipts`' docstrings).
    """
    scrubbed_args = _scrub_never_model_facing_args(kwargs)
    try:
        raw_result = await real_fn.invoke(arguments=scrubbed_args, skip_parsing=True)
    except ToolExecutionException as exc:
        blocked = _parse_execution_blocked_from_exception(exc)
        if blocked is None:
            # Not the real, recognized denial shape -- some other failure (a transport error, a
            # malformed decision_id, ...) that this package has no special knowledge of. Let it
            # propagate as the ordinary MAF tool failure it already is, rather than pretending
            # it's a governance denial this package understands.
            raise
        hint = f" ({blocked.override_hint})" if blocked.override_hint else ""
        raise AlgentaToolDenied(
            f"Algenta tool {real_fn.name!r} was blocked by the real, synchronous {blocked.gate!r} "
            f"policy gate -- {blocked.message}{hint}",
            blocked=blocked,
        ) from exc

    payload = _extract_function_result_payload(raw_result)
    receipt = parse_execution_receipt(payload, model=receipt_model)
    if receipt is None:
        raise AlgentaToolExecutionFailed(
            f"Algenta tool {real_fn.name!r} returned a non-error result that does not validate "
            f"as the real ExecutionReceipt shape: {payload!r}"
        )
    return raw_result


def _wrap_mcp_function(real_fn: FunctionTool, *, receipt_model: type[ExecutionReceipt]) -> FunctionTool:
    """Build the model-facing `FunctionTool` for one already-profile-allowed real MCP function.

    Rebuilds a fresh `FunctionTool` (rather than mutating `real_fn` in place) whose `func`:

    1. Scrubs `NEVER_MODEL_FACING_FIELDS` from the model-supplied arguments (the call-time layer
       behind the schema-level scrub below -- see this module's docstring for why both matter).
    2. Calls the real underlying function with `skip_parsing=True`, so this wrapper sees the raw
       result exactly as the real MCP call produced it, before MAF's own `FunctionTool.invoke`
       would otherwise re-parse it into `list[Content]` a second time.
    3. For `execute_decision` only: maps the real success/denial shape onto a real
       `ExecutionReceipt` or an `AlgentaToolDenied`/`AlgentaToolExecutionFailed` -- see
       `_governed_execute_decision_call`. Every other tool's raw result passes straight through,
       scrubbed but otherwise unexamined -- this package has no verified receipt/denial contract
       for anything but `execute_decision`.
    """
    schema = _strip_never_model_facing_schema(dict(real_fn.parameters()))

    if real_fn.name == EXECUTE_DECISION:

        async def _call(**kwargs: Any) -> Any:
            return await _governed_execute_decision_call(real_fn, receipt_model=receipt_model, **kwargs)
    else:

        async def _call(**kwargs: Any) -> Any:
            scrubbed_args = _scrub_never_model_facing_args(kwargs)
            return await real_fn.invoke(arguments=scrubbed_args, skip_parsing=True)

    return FunctionTool(
        name=real_fn.name,
        description=real_fn.description or "",
        input_model=schema,
        func=_call,
    )


def _wrap_functions(
    functions: list[FunctionTool], *, profile: ToolProfile, receipt_model: type[ExecutionReceipt]
) -> list[FunctionTool]:
    allowed_names = resolve_profile_tool_names(profile, available_tool_names=frozenset(fn.name for fn in functions))
    return [_wrap_mcp_function(fn, receipt_model=receipt_model) for fn in functions if fn.name in allowed_names]


@asynccontextmanager
async def create_algenta_tools(
    *,
    base_url: str | None = None,
    profile: ToolProfile = DEFAULT_PROFILE,
    mcp_tool: _ConnectedMCPTool | None = None,
    server_name: str = _DEFAULT_SERVER_NAME,
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    **mcp_kwargs: Any,
) -> AsyncIterator[list[FunctionTool]]:
    """Build a governed-execution-aware list of `FunctionTool`s from a self-hosted Algenta engine.

    An **async context manager**, not a plain async function -- deliberately mirroring
    `agent_framework.MCPStreamableHTTPTool`'s own `async with ... as mcp_tool:` idiom, since this
    function's default path (no `mcp_tool=` given) owns exactly one such connection for the
    lifetime of the `with` block:

    ```python
    from agent_framework import Agent
    from maf_algenta import create_algenta_tools

    async with create_algenta_tools(profile="observe") as tools:
        agent = Agent(my_chat_client, tools=tools)
        result = await agent.run("What's the expected value of scenario X?")
    ```

    Args:
        base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
            `ALGENTA_BASE_URL` environment variable, falling back to
            `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta cloud
            default). Ignored if `mcp_tool` is given.
        profile: Which tool profile to expose to the model -- one of `"observe"` (default),
            `"govern"`, `"execute"`, or `"full"`. See
            [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
        mcp_tool: Advanced escape hatch / test seam: an already-connected `MCPTool` (or anything
            exposing a `.functions` list of `FunctionTool`s shaped the same way -- see
            `tests/test_profile_filtering.py`'s fake registry) to filter and wrap instead of
            having this function build and own a connection itself. **You are responsible for
            that value's own connection lifecycle** (connecting it before entering this context
            manager, and closing it after) -- this function only reads `.functions` off it and
            never calls `.connect()`/`.close()` on it. Mutually exclusive with `base_url` and
            `**mcp_kwargs`.
        server_name: The name to register this connection under, when this function builds its
            own `MCPStreamableHTTPTool`. Defaults to `"algenta"`. Ignored if `mcp_tool` is given.
        receipt_model: The `ExecutionReceipt` subclass to validate a successful `execute_decision`
            call's result against. Override if your engine's receipt envelope has grown fields you
            want typed (the base model already accepts and preserves unknown fields via
            `extra="allow"`, so most callers won't need this).
        **mcp_kwargs: Any other `agent_framework.MCPStreamableHTTPTool` constructor keyword
            argument (e.g. `headers`, `http_client`, `request_timeout`), forwarded as-is when
            this function builds its own connection. Ignored (and rejected, see below) if
            `mcp_tool` is given.

    Yields:
        The tools allowed under `profile`, each with `force`/`override_safety` stripped from its
        advertised schema and scrubbed from its call-time arguments. Calling `execute_decision`
        either returns a real `ExecutionReceipt` or raises `AlgentaToolDenied` /
        `AlgentaToolExecutionFailed` -- see this module's docstring.

    Raises:
        ValueError: If `profile` isn't one of the four contract profiles, or if `mcp_tool` is
            given together with `base_url` or any `**mcp_kwargs`.
    """
    if profile not in TOOL_PROFILES:
        raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")

    connection_kwargs_given = base_url is not None or bool(mcp_kwargs)
    if mcp_tool is not None and connection_kwargs_given:
        raise ValueError(
            "Pass either `mcp_tool` or the MCP-connection arguments (`base_url`/**mcp_kwargs), not both."
        )

    if mcp_tool is not None:
        yield _wrap_functions(mcp_tool.functions, profile=profile, receipt_model=receipt_model)
        return

    resolved_base_url = base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
    # `allowed_tools=None` for `"full"` means "don't filter at the MCP layer at all" -- matching
    # the contract's own `tools: "*"` sentinel; `_wrap_functions`' own post-hoc filtering below
    # would otherwise have nothing left to narrow for that profile anyway.
    allowed = None if TOOL_PROFILES[profile] == "*" else sorted(TOOL_PROFILES[profile])
    async with MCPStreamableHTTPTool(
        name=server_name, url=resolved_base_url, allowed_tools=allowed, **mcp_kwargs
    ) as owned_mcp_tool:
        yield _wrap_functions(owned_mcp_tool.functions, profile=profile, receipt_model=receipt_model)


__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "create_algenta_tools",
]
