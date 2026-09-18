"""`create_algenta_tools` -- a governed-execution-aware `list[FunctionTool]` wrapping a
self-hosted Algenta Engine's MCP tool surface, built on `llama-index-tools-mcp`'s own real
`BasicMCPClient` (a real, separate PyPI package -- confirmed directly from its installed
`.dist-info/METADATA` that its only dependencies are `llama-index-core`, `mcp`, and `pydantic`;
the full `llama-index` metapackage is never required). Per the plan's own "LlamaIndex consumes MCP
in workflows" framing, this package's job is Algenta's governance value-add layered on top of
LlamaIndex's real, existing MCP support -- not a from-scratch MCP client.

**Why this module does not simply call `McpToolSpec(client=...).to_tool_list_async()` and wrap the
result**, unlike how naturally that would read: `McpToolSpec._create_tool_fn`'s closure just
forwards kwargs straight to `self.client.call_tool(tool_name, kwargs)` with no scrubbing and no
outcome mapping at all. This module needs two things that closure doesn't give it, for every
allowed tool call:

1. **Two-layer never-model-facing scrubbing**: `force`/`override_safety` stripped from the
   advertised schema *and* from the arguments dict actually forwarded to `call_tool(...)`.
2. **The `execute_decision` outcome mapping**: a successful call returns a typed
   `ExecutionReceipt`; a call blocked by one of the three real named policy gates raises
   `AlgentaToolDenied`; anything else that isn't a recognized success raises
   `AlgentaToolExecutionFailed`. See `llamaindex_algenta.exceptions` for the full accounting.

So this module builds its own `async def wrapper(**kwargs)` per allowed tool and hands that to
`FunctionTool.from_defaults(...)` directly -- reusing only `McpToolSpec`'s public
JSON-Schema-to-pydantic-model helpers (`create_model_from_json_schema`, `remove_model_fields`),
never its tool-call closures or `fetch_tools`/`to_tool_list_async`.

**A previous version of this module also built `wrapper` with a `ctx: Context` parameter**, so it
could call `Context.wait_for_event()` -- llama-index-workflows' real human-in-the-loop pause
primitive -- when a receipt came back with a fictional, asynchronous `approval_state == "pending"`
that does not exist on the real `execute_decision` tool (see `llamaindex_algenta.exceptions`'
module docstring for the full accounting of what replaced it). That pause is gone: the real tool
either succeeds or is denied synchronously in the same call, so there is nothing left to pause on.
With no `ctx: Context`-annotated parameter left anywhere in this module, the one documented reason
this module used to omit `from __future__ import annotations` (unlike every sibling module in this
package) no longer applies -- `FunctionTool.__init__`'s `requires_context` detection never inspects
this module's functions at all now, since none of them declare a `Context` parameter. This module
now uses `from __future__ import annotations` like every sibling, and requires no comment
explaining why it doesn't.

Layers two things on top of each already-profile-allowed real MCP tool call:

1. **Tool-profile filtering** (`profile=`): only the tool names `contracts/integration-tool-contract.json`
   (https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
   assigns to the requested profile are ever turned into a `FunctionTool` at all. Unlike
   Haystack's `MCPToolset(tool_names=...)`, the raw MCP protocol's `list_tools()` has no
   server-side name filter to layer this on top of, so this is a single client-side filter over
   whatever the connected engine's `list_tools()` actually returns.
2. **Two-layer never-model-facing scrubbing and the `execute_decision` outcome mapping**,
   described above.

**Why this module does not need any connection lifecycle (`connect()`/`close()`)**, unlike every
other sibling package's toolset module: verified live from `BasicMCPClient`'s real source
(`llama_index/tools/mcp/client.py`) that every public method (`list_tools`, `call_tool`, ...) opens
and tears down its own MCP session independently via `_run_session()`'s `async with` -- there is no
persistent connection this module builds and would need to own the lifecycle of. A caller who
wants connection reuse/pooling can pass their own pre-built `client=` (see `create_algenta_tools`).
"""

from __future__ import annotations

import os
from typing import Any

from llama_index.core.tools import FunctionTool
from llama_index.core.tools.types import ToolMetadata
from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

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
    ExecutionDenial,
    ExecutionReceipt,
    call_error_text,
    is_call_error,
    parse_execution_denial,
    parse_execution_receipt,
    unwrap_call_tool_result,
)

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=` argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"


def _scrub_never_model_facing_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    if not (NEVER_MODEL_FACING_FIELDS & kwargs.keys()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k not in NEVER_MODEL_FACING_FIELDS}


def _resolve_call(
    *,
    tool_name: str,
    raw: Any,
    receipt_model: type[ExecutionReceipt],
    denial_model: type[ExecutionDenial],
) -> Any:
    """Turn one raw `client.call_tool(...)` result into whatever this tool call should return to
    the model, or raise `AlgentaToolDenied`/`AlgentaToolExecutionFailed`.

    Only `execute_decision` gets the typed receipt/denial treatment -- every other tool's result
    is unwrapped and passed through unchanged (after the same MCP protocol-level error check),
    since no other tool in the real contract is safety-critical or gated. See this module's and
    `llamaindex_algenta.receipts`' docstrings for the full accounting.
    """
    payload = unwrap_call_tool_result(raw)

    if tool_name == EXECUTE_DECISION:
        denial = parse_execution_denial(payload, model=denial_model)
        if denial is not None:
            raise AlgentaToolDenied(
                f"Algenta tool {tool_name!r} was blocked by the {denial.gate!r} gate: {denial.message}",
                gate=denial.gate,
                code=denial.code,
                override_hint=denial.override_hint,
            )

        receipt = parse_execution_receipt(payload, model=receipt_model)
        if receipt is not None:
            return receipt

    if is_call_error(raw):
        # MCP protocol-level failure: the connected server's own tool implementation raised for a
        # reason that isn't one of the three named execution gates (those come back as an
        # ordinary, well-formed denial body, not a protocol-level error), and the MCP SDK already
        # turned that into ordinary (non-raising) response data. Treat it as a failure -- never
        # silently pass it through as if it were an ordinary, non-governed success.
        raise AlgentaToolExecutionFailed(
            f"Algenta tool {tool_name!r} failed at the MCP protocol level: {call_error_text(raw)}"
        )

    if tool_name == EXECUTE_DECISION:
        # Reached only if `execute_decision`'s result was neither a well-formed `ExecutionReceipt`
        # nor a well-formed `ExecutionDenial` nor a protocol-level error -- an unexpected shape
        # from a real engine. Fail loudly rather than silently passing through a shape this
        # package doesn't understand for its one safety-critical tool.
        raise AlgentaToolExecutionFailed(
            f"Algenta tool {tool_name!r} returned a result that is neither a recognized "
            f"ExecutionReceipt nor a recognized named-gate denial: {payload!r}"
        )

    # Every other tool is a plain, non-gated passthrough -- return whatever the engine sent back,
    # unchanged.
    return payload if payload is not None else raw


def _build_tool(
    *,
    client: Any,
    raw_tool: Any,
    schema_builder: McpToolSpec,
    receipt_model: type[ExecutionReceipt],
    denial_model: type[ExecutionDenial],
) -> FunctionTool:
    """Build one outcome-aware `FunctionTool` for `raw_tool` (an `mcp.types.Tool`).

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

    async def wrapper(**kwargs: Any) -> Any:
        raw = await client.call_tool(tool_name, _scrub_never_model_facing_args(kwargs))
        return _resolve_call(tool_name=tool_name, raw=raw, receipt_model=receipt_model, denial_model=denial_model)

    metadata = ToolMetadata(name=tool_name, description=raw_tool.description or "", fn_schema=model_schema)
    return FunctionTool.from_defaults(async_fn=wrapper, tool_metadata=metadata)


async def create_algenta_tools(
    *,
    base_url: str | None = None,
    profile: ToolProfile = DEFAULT_PROFILE,
    client: Any | None = None,
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    denial_model: type[ExecutionDenial] = ExecutionDenial,
    **client_kwargs: Any,
) -> list[FunctionTool]:
    """Build the outcome-aware `FunctionTool` list for a self-hosted Algenta Engine.

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
            `"govern"`, `"execute"`, or `"full"`. See `contracts/integration-tool-contract.json`:
            https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json
        client: Advanced escape hatch / test seam: an already-constructed MCP client to use
            instead of having this function build a `BasicMCPClient` from `base_url`. Must
            support the same duck-typed async surface `McpToolSpec` itself requires:
            `list_tools()` and `call_tool(name, arguments)`. Mutually exclusive with `base_url`
            and `**client_kwargs`. **You own this value's own lifecycle** (this function only
            ever calls `list_tools()`/`call_tool()` on it).
        receipt_model: The `ExecutionReceipt` subclass to validate a successful `execute_decision`
            result against.
        denial_model: The `ExecutionDenial` subclass to validate a blocked `execute_decision`
            result against.
        **client_kwargs: Any other `BasicMCPClient` constructor keyword argument (e.g. `headers`,
            `timeout`, `auth`), forwarded as-is when this function builds its own client. Ignored
            (and rejected, see below) if `client` is given.

    Returns:
        One `FunctionTool` per tool name `profile` allows that the connected engine's `list_tools()`
        actually advertises right now, each with `force`/`override_safety` stripped from its
        advertised schema and scrubbed from its call-time arguments, and `execute_decision`
        additionally carrying the real receipt/denial outcome mapping described in this module's
        docstring.

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
            denial_model=denial_model,
        )
        for raw_tool in raw_tools
        if raw_tool.name in allowed_names
    ]


__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "create_algenta_tools",
]
