"""`create_algenta_tools` -- an async context manager yielding a governed-execution-aware list of
`agent_framework.FunctionTool`s from a self-hosted Algenta Engine's MCP tool surface.

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
3. **The governed-execution receipt mapping** (`_governed_call`, inside `_wrap_mcp_function`):
   every real call's result is parsed into a `GovernedExecutionReceipt`; `execute_decision`
   additionally gets `approval_mode="always_require"` on its exposed `FunctionTool` -- MAF's real,
   native pre-call human-in-the-loop gate (`Content(type="function_approval_request")`, verified
   live against the installed package) -- and a receipt that is still `"pending"` *after* that
   gate has already been satisfied, or that the engine denies/fails outright, raises one of
   `maf_algenta.exceptions`' three `agent_framework.MiddlewareFailure` subclasses. See that
   module's docstring, and the package README's "Approval mapping" section, for the full
   reasoning behind building this on `MiddlewareFailure` rather than a bespoke exception
   hierarchy or (nonexistent) resumable pause.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from agent_framework import FunctionTool, MCPStreamableHTTPTool

from .contract import (
    DEFAULT_PROFILE,
    EXECUTE_DECISION,
    NEVER_MODEL_FACING_FIELDS,
    TOOL_PROFILES,
    ToolProfile,
    resolve_profile_tool_names,
)
from .exceptions import AlgentaApprovalStillPending, AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import GovernedExecutionReceipt, parse_receipt

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
    `structuredContent`-first preference. Returns `None` when nothing recognizable is found --
    the deliberate signal `parse_receipt` uses to treat a result as a non-governed passthrough
    (e.g. `get_contract`'s discovery payload).
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


def _wrap_mcp_function(
    real_fn: FunctionTool, *, receipt_model: type[GovernedExecutionReceipt]
) -> FunctionTool:
    """Build the model-facing `FunctionTool` for one already-profile-allowed real MCP function.

    Rebuilds a fresh `FunctionTool` (rather than mutating `real_fn` in place) whose `func`:

    1. Scrubs `NEVER_MODEL_FACING_FIELDS` from the model-supplied arguments (the call-time layer
       behind the schema-level scrub below -- see this module's docstring for why both matter).
    2. Calls the real underlying function with `skip_parsing=True`, so this wrapper sees the raw
       result exactly as the real MCP call produced it, before MAF's own `FunctionTool.invoke`
       would otherwise re-parse it into `list[Content]` a second time.
    3. Parses that result into a `GovernedExecutionReceipt` (via `_extract_function_result_payload`
       + `parse_receipt`) and maps `approval_state`/`code`/`status` onto success / one of the
       three `AlgentaGovernedCallFailure` subclasses -- see `maf_algenta.exceptions`.

    `execute_decision` additionally gets `approval_mode="always_require"`: MAF's real pre-call
    human-in-the-loop gate, checked by the model in `agent.run()`'s own function-invocation loop
    *before* this wrapper's `func` is ever called -- see this module's docstring.
    """
    schema = _strip_never_model_facing_schema(dict(real_fn.parameters()))

    async def _governed_call(**kwargs: Any) -> Any:
        scrubbed_args = _scrub_never_model_facing_args(kwargs)
        raw_result = await real_fn.invoke(arguments=scrubbed_args, skip_parsing=True)
        payload = _extract_function_result_payload(raw_result)
        receipt = parse_receipt(payload, model=receipt_model)
        if receipt is None:
            return raw_result

        if receipt.is_pending_approval():
            raise AlgentaApprovalStillPending(
                f"Algenta tool {real_fn.name!r} is still pending server-side policy approval "
                f"(plan_hash={receipt.plan_hash!r}) even though the model's request to call it "
                "already passed MAF's own approval_mode gate -- the two are orthogonal (see "
                "the package README). Record the real approval against this plan_hash out of "
                "band, then retry the call from a fresh turn.",
                receipt=receipt,
            )
        if receipt.is_denied():
            raise AlgentaToolDenied(
                f"Algenta tool {real_fn.name!r} was denied by policy -- {receipt.denial_reason()}",
                receipt=receipt,
            )
        if not receipt.is_success():
            raise AlgentaToolExecutionFailed(
                f"{receipt.code}: Algenta tool {real_fn.name!r} did not complete successfully "
                f"(status={receipt.status!r}).",
                receipt=receipt,
            )
        return raw_result

    return FunctionTool(
        name=real_fn.name,
        description=real_fn.description or "",
        input_model=schema,
        approval_mode="always_require" if real_fn.name == EXECUTE_DECISION else None,
        func=_governed_call,
    )


def _wrap_functions(
    functions: list[FunctionTool], *, profile: ToolProfile, receipt_model: type[GovernedExecutionReceipt]
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
    receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt,
    **mcp_kwargs: Any,
) -> AsyncIterator[list[FunctionTool]]:
    """Build a governed-execution-aware list of `FunctionTool`s from a self-hosted Algenta Engine.

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
        receipt_model: The `GovernedExecutionReceipt` subclass to validate tool results against.
            Override if your engine's receipt envelope has grown fields you want typed (the base
            model already accepts and preserves unknown fields via `extra="allow"`, so most
            callers won't need this).
        **mcp_kwargs: Any other `agent_framework.MCPStreamableHTTPTool` constructor keyword
            argument (e.g. `headers`, `http_client`, `request_timeout`), forwarded as-is when
            this function builds its own connection. Ignored (and rejected, see below) if
            `mcp_tool` is given.

    Yields:
        The tools allowed under `profile`, each with `force`/`override_safety` stripped from its
        advertised schema and scrubbed from its call-time arguments, and `execute_decision` (if
        present under `profile`) gated by `approval_mode="always_require"`.

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
