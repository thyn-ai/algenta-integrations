"""`create_algenta_tools` -- a governed-execution-aware `haystack.tools.toolset.Toolset` wrapping a
self-hosted Algenta Engine's MCP tool surface, built on Haystack's own real
`haystack_integrations.tools.mcp.MCPToolset` (a real, separate PyPI package, `mcp-haystack` --
confirmed by grepping an installed `haystack-ai` 3.0.0 tree for any `mcp` module: none exists
there. See the package README's "Why `mcp-haystack`, not just `haystack-ai`" section).

Structural template: `MCPToolset` itself, used the way its own docs and docstrings use it --
construct it with `tool_names=[...]` for server-side filtering, then `warm_up()` before use. There
is no async-context-manager idiom to mirror here (unlike `agent_framework.MCPStreamableHTTPTool`,
which `maf_algenta.toolset` deliberately does mirror with its own `async with`): `MCPToolset`'s own
real lifecycle is synchronous construction + `warm_up()` + an explicit `close()`, confirmed by
reading the installed `mcp-haystack` 1.4.1 source directly (`_connect_and_load_tools` bridges the
real async MCP client through its own internal `AsyncExecutor`, so every public method on
`MCPToolset` -- including tool invocation itself -- is an ordinary blocking call). `create_algenta_tools`
mirrors that lifecycle exactly: it is a plain, synchronous function, and the `AlgentaToolset` it
returns carries a `close()` of its own that forwards to the `MCPToolset` connection this function
built and owns (a no-op when the caller supplied one of the escape hatches below, mirroring every
other sibling package's "you own what you hand in" convention).

Layers two things on top of each already-profile-allowed real MCP tool call (**not** three, unlike
every sibling in this repository -- see below for why):

1. **Tool-profile filtering** (`profile=`): only the tool names
   [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
   assigns to the requested profile are exposed to the model -- enforced twice: once at
   construction time via `MCPToolset(tool_names=...)` (so the server-advertised tool list this
   package even sees is already filtered -- this is Haystack's own real, native, fully-adequate
   mechanism; no wrapper is needed for this half at all, and it is also the documented mitigation
   Haystack's own `MCPToolset` docs point at deepset's stated "20-30 tools can overwhelm the LLM's
   tool resolution logic" warning), and again as a defense-in-depth pass over whatever
   `get_selectable_tools()` actually returns (`resolve_profile_tool_names`), which is what the
   `mcp_toolset=`/`tools=` escape hatches below (a toolset or tool list this function did not
   build itself) rely on for their own filtering.
2. **Two-layer never-model-facing scrubbing**: `force`/`override_safety` are stripped from every
   wrapped tool's advertised schema (`Tool.parameters`, a plain mutable JSON-Schema `dict`) *and*
   from the arguments dict actually forwarded to the real underlying MCP call. Both layers matter
   independently, for the same reason `maf_algenta` documents for its own two-layer scrub: real
   MCP-derived schemas do not set `"additionalProperties": false`, so a model (or a caller) that
   still supplied `force`/`override_safety` as an argument would sail straight past schema-level
   omission and reach the real call unless the call-time scrub also strips it.

**What is deliberately NOT here, unlike every other sibling package's toolset/interceptor module:
the governed-execution receipt mapping.** `Tool.invoke()`/`invoke_async()` unconditionally catches
any exception raised inside a tool's own `function`/`async_function` and re-raises the generic
`haystack.tools.errors.ToolInvocationError` instead -- and, going through a real `Agent` with its
documented default `raise_on_tool_invocation_failure=False`, that gets silently swallowed into an
ordinary tool-result `ChatMessage` fed back to the model, never raised out of `agent.run()` at all
(verified live -- see the package README). Raising `AlgentaToolDenied` from inside this module's
wrapped calls would therefore be exactly the wrong seam for the common, real-`Agent` case. That mapping lives instead in
`haystack_algenta.hooks.GovernedReceiptHook`, a Haystack `after_tool` hook -- the one seam proven
live to let a custom exception type survive `agent.run()` completely unmodified. See that module's
docstring, and the package README's "Approval mapping" section, for the full reasoning.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from haystack.tools import Tool, Toolset
from haystack_integrations.tools.mcp import MCPServerInfo, MCPToolset, StreamableHttpServerInfo

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile, resolve_profile_tool_names

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=` argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"


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


def _wrap_tool(tool: Tool) -> Tool:
    """Rebuild `tool` (already profile-filtered) with `NEVER_MODEL_FACING_FIELDS` stripped from
    its advertised schema and scrubbed from its call-time arguments.

    Rebuilds a fresh `Tool` via `dataclasses.replace` (rather than mutating `tool` in place --
    `Tool` is a plain `@dataclass`, the idiomatic move) whose `function`/`async_function` scrub
    `NEVER_MODEL_FACING_FIELDS` out of the model-supplied keyword arguments before forwarding the
    call to the real, original callable. `MCPToolset`-built tools only ever set `function` (a sync
    closure bridging through its own internal `AsyncExecutor` -- confirmed by reading the
    installed `mcp-haystack` 1.4.1 source directly), but both are handled for the `tools=`/
    `mcp_toolset=` escape hatches, which may hand back a tool built any way at all.

    Deliberately does *not* touch `result` at all -- no receipt parsing, no raising. See this
    module's own docstring for why that mapping belongs in `haystack_algenta.hooks` instead.
    """
    new_schema = _strip_never_model_facing_schema(tool.parameters)

    new_function = None
    new_async_function = None
    if tool.function is not None:
        real_fn = tool.function

        def new_function(**kwargs: Any) -> Any:
            return real_fn(**_scrub_never_model_facing_args(kwargs))

    if tool.async_function is not None:
        real_async_fn = tool.async_function

        async def new_async_function(**kwargs: Any) -> Any:
            return await real_async_fn(**_scrub_never_model_facing_args(kwargs))

    return replace(tool, parameters=new_schema, function=new_function, async_function=new_async_function)


class AlgentaToolset(Toolset):
    """The `Toolset` `create_algenta_tools` returns: an ordinary Haystack `Toolset` (so it drops
    straight into `Agent(tools=...)` or a `Pipeline`, and behaves like one -- iterable, supports
    `in`, `len()`, etc., all inherited unchanged from `haystack.tools.toolset.Toolset`) plus a
    `close()` that tears down the `MCPToolset` connection this package built and owns, when it
    built one.

    Not serializable (`to_dict`/`from_dict` raise): its tools carry closures scrubbing
    `NEVER_MODEL_FACING_FIELDS` at call time, which cannot round-trip through Haystack's
    to_dict/from_dict component-serialization protocol the way a plain, statically-defined
    `Toolset` can. Rebuild with `create_algenta_tools(...)` again instead of trying to
    deserialize one of these.
    """

    def __init__(self, tools: list[Tool], *, owned_mcp_toolset: MCPToolset | None = None) -> None:
        super().__init__(tools=tools)
        self._owned_mcp_toolset = owned_mcp_toolset

    def close(self) -> None:
        """Close the underlying MCP connection, if this toolset built and owns one.

        A no-op for the `mcp_toolset=`/`tools=` escape hatches -- matching every sibling package's
        convention that the caller owns the lifecycle of any connection/tool list they supplied
        themselves.
        """
        if self._owned_mcp_toolset is not None:
            self._owned_mcp_toolset.close()

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError(
            "AlgentaToolset cannot be serialized (its tools carry non-serializable scrubbing "
            "closures) -- rebuild it with create_algenta_tools(...) instead."
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlgentaToolset":
        raise NotImplementedError(
            "AlgentaToolset cannot be deserialized -- rebuild it with create_algenta_tools(...) instead."
        )


def create_algenta_tools(
    *,
    base_url: str | None = None,
    profile: ToolProfile = DEFAULT_PROFILE,
    mcp_toolset: MCPToolset | None = None,
    tools: list[Tool] | None = None,
    connection_timeout: float = 30.0,
    invocation_timeout: float = 30.0,
    **mcp_toolset_kwargs: Any,
) -> AlgentaToolset:
    """Build a governed-execution-aware `AlgentaToolset` from a self-hosted Algenta Engine.

    ```python
    from haystack.components.agents import Agent
    from haystack.components.generators.chat import OpenAIChatGenerator
    from haystack_algenta import create_algenta_tools

    toolset = create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")
    agent = Agent(chat_generator=OpenAIChatGenerator(), tools=toolset)
    result = agent.run(messages=[...])
    toolset.close()
    ```

    Args:
        base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
            `ALGENTA_BASE_URL` environment variable, falling back to
            `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta cloud
            default). Ignored if `mcp_toolset` or `tools` is given.
        profile: Which tool profile to expose to the model -- one of `"observe"` (default),
            `"govern"`, `"execute"`, or `"full"`. See
            [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
        mcp_toolset: Advanced escape hatch / test seam: an already-constructed (and, if you want
            its tools available immediately, already-`warm_up()`-ed) `MCPToolset` to filter and
            wrap instead of having this function build and own a connection itself. **You are
            responsible for that value's own connection lifecycle** (this function only ever
            calls `get_selectable_tools()` on it, never `warm_up()`/`close()`). Mutually exclusive
            with `tools`, `base_url`, and `**mcp_toolset_kwargs`.
        tools: Advanced escape hatch / test seam: a pre-built, in-memory list of `Tool`s to filter
            and wrap instead of connecting over MCP at all -- e.g. a fake registry in a test, with
            no MCP connection whatsoever. Mutually exclusive with `mcp_toolset` and the
            MCP-connection arguments.
        connection_timeout: Forwarded to `MCPToolset`. Ignored if `mcp_toolset` or `tools` is given.
        invocation_timeout: Forwarded to `MCPToolset`. Ignored if `mcp_toolset` or `tools` is given.
        **mcp_toolset_kwargs: Any other `MCPToolset` constructor keyword argument (e.g.
            `inputs_from_state`, `outputs_to_state`, `outputs_to_string`), forwarded as-is when
            this function builds its own connection. Ignored (and rejected, see below) if
            `mcp_toolset` or `tools` is given.

    Returns:
        An `AlgentaToolset` containing the tools allowed under `profile`, each with
        `force`/`override_safety` stripped from its advertised schema and scrubbed from its
        call-time arguments. Call `.close()` on it when you are done, to tear down the connection
        this function built (a no-op if you supplied `mcp_toolset=`/`tools=` yourself).

    Raises:
        ValueError: If `profile` isn't one of the four contract profiles, or if more than one of
            `mcp_toolset`, `tools`, and the MCP-connection arguments (`base_url`/`**mcp_toolset_kwargs`)
            are given together.
    """
    if profile not in TOOL_PROFILES:
        raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")

    connection_kwargs_given = base_url is not None or bool(mcp_toolset_kwargs)
    escape_hatches_given = sum(x is not None for x in (mcp_toolset, tools))
    if escape_hatches_given > 1:
        raise ValueError("Pass at most one of `mcp_toolset` or `tools`, not both.")
    if escape_hatches_given and connection_kwargs_given:
        raise ValueError(
            "Pass either an escape hatch (`mcp_toolset`/`tools`) or the MCP-connection arguments "
            "(`base_url`/**mcp_toolset_kwargs), not both."
        )

    if tools is not None:
        allowed_names = resolve_profile_tool_names(profile, available_tool_names=frozenset(t.name for t in tools))
        wrapped = [_wrap_tool(t) for t in tools if t.name in allowed_names]
        return AlgentaToolset(tools=wrapped)

    if mcp_toolset is not None:
        selectable = mcp_toolset.get_selectable_tools()
        allowed_names = resolve_profile_tool_names(profile, available_tool_names=frozenset(t.name for t in selectable))
        wrapped = [_wrap_tool(t) for t in selectable if t.name in allowed_names]
        return AlgentaToolset(tools=wrapped)

    resolved_base_url = base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
    # `tool_names=None` for `"full"` means "don't filter at the MCP layer at all" -- matching the
    # contract's own `tools: "*"` sentinel; the defense-in-depth `resolve_profile_tool_names` call
    # below would otherwise have nothing left to narrow for that profile anyway.
    allowed = None if TOOL_PROFILES[profile] == "*" else sorted(TOOL_PROFILES[profile])
    server_info: MCPServerInfo = StreamableHttpServerInfo(url=resolved_base_url)
    owned = MCPToolset(
        server_info=server_info,
        tool_names=allowed,
        connection_timeout=connection_timeout,
        invocation_timeout=invocation_timeout,
        **mcp_toolset_kwargs,
    )
    owned.warm_up()
    selectable = owned.get_selectable_tools()
    allowed_names = resolve_profile_tool_names(profile, available_tool_names=frozenset(t.name for t in selectable))
    wrapped = [_wrap_tool(t) for t in selectable if t.name in allowed_names]
    return AlgentaToolset(tools=wrapped, owned_mcp_toolset=owned)


__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "AlgentaToolset",
    "create_algenta_tools",
]
