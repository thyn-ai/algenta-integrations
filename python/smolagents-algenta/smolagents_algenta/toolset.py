"""`AlgentaToolset` -- a governed-execution-aware smolagents `Tool` collection wrapping a
self-hosted Algenta engine's MCP tool surface.

`execute_decision` is fully synchronous: a call either succeeds (a typed `ExecutionReceipt`) or
is blocked in the same call by exactly one of three named policy gates (a 409-shaped
`ExecutionDenial`) -- there is no asynchronous "pending approval" state to model. A blocked call
surfaces through smolagents' own mechanism as a raised
[`AgentToolExecutionError`][smolagents.AgentToolExecutionError] carrying the named gate and the
engine's own message/override hint, since smolagents has no dedicated "denied" primitive distinct
from a tool execution failure. The gate name is preserved in the error message so callers and
agents can distinguish policy blocks from transport or code failures.

The FastMCP MCP client is async, but smolagents calls `Tool.forward` synchronously. This toolset
runs the FastMCP client on a dedicated background event loop and bridges synchronous
`Tool.forward` calls to it, so the public API stays synchronous -- matching how smolagents agents
consume tools -- without requiring callers to manage the loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Sequence
from typing import Any

from fastmcp import Client
from smolagents import AgentLogger, AgentToolExecutionError, Tool

from .contract import (
    DEFAULT_PROFILE,
    NEVER_MODEL_FACING_FIELDS,
    ToolProfile,
    resolve_profile_tool_names,
)
from .receipts import ExecutionReceipt, parse_denial, parse_receipt

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=`
#: constructor argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"

#: Logger passed to `AgentToolExecutionError` so policy-gate denials surface through smolagents'
#: own error type without requiring every tool wrapper to carry an agent-level logger.
_ERROR_LOGGER = AgentLogger()


class _BackgroundMCPClient:
    """A synchronous handle around an async `fastmcp.Client` running on a background loop.

    This isolates the MCP connection from the caller's event loop (if any), so smolagents'
    synchronous `Tool.forward` can call async MCP operations without deadlocking an async test
    runner or requiring the user to write async glue code.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._client: Client | None = None

    def connect(self) -> None:
        """Start the background loop, enter the MCP client, and initialize the session."""
        if self._thread is not None:
            return

        created = threading.Event()

        def run_loop() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            created.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run_loop, daemon=True)
        self._thread.start()
        created.wait(timeout=5.0)
        if self._loop is None:
            raise RuntimeError("Background MCP client loop failed to start")

        # Confirm the loop is actually processing events before attempting I/O.
        self._run(asyncio.sleep(0))

        self._client = Client(self._base_url)
        self._run(self._client.__aenter__())
        self._run(self._client.initialize())

    def disconnect(self) -> None:
        """Close the MCP client and stop the background loop."""
        if self._client is not None and self._loop is not None:
            self._run(self._client.__aexit__(None, None, None))
            self._client = None

        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def list_tools(self) -> list[Any]:
        """List tools from the connected MCP server."""
        if self._client is None:
            raise RuntimeError("MCP client is not connected")
        return self._run(self._client.list_tools())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool by name and return the `CallToolResult`."""
        if self._client is None:
            raise RuntimeError("MCP client is not connected")
        return self._run(self._client.call_tool(name, arguments))

    def _run(self, coro: Any) -> Any:
        """Run a coroutine on the background loop and block for the result."""
        if self._loop is None:
            raise RuntimeError("Background MCP client loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30.0)


def _map_json_schema_type(json_schema_type: str) -> str:
    """Map a JSON Schema type to the closest smolagents `AUTHORIZED_TYPES` string."""
    mapping = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
        "null": "null",
    }
    return mapping.get(json_schema_type, "any")


def _mcp_schema_to_inputs(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert an MCP tool's JSON Schema input definition to smolagents' `inputs` shape.

    Strips `NEVER_MODEL_FACING_FIELDS` from the advertised schema so the model never sees them as
    available parameters. Required parameters are left required; optional parameters are marked
    nullable so smolagents' argument validation matches the schema.
    """
    inputs: dict[str, dict[str, Any]] = {}
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    for name, prop in properties.items():
        if name in NEVER_MODEL_FACING_FIELDS:
            continue
        prop = prop if isinstance(prop, dict) else {}
        input_def: dict[str, Any] = {}

        json_type = prop.get("type", "any")
        nullable = False
        if isinstance(json_type, list):
            non_null = [t for t in json_type if t != "null"]
            if "null" in json_type:
                nullable = True
            json_type = non_null[0] if non_null else "any"

        input_def["type"] = _map_json_schema_type(json_type)
        if nullable or name not in required:
            input_def["nullable"] = True

        description = prop.get("description") or prop.get("title") or name
        input_def["description"] = description
        inputs[name] = input_def

    return inputs


def _strip_never_model_facing_inputs(
    inputs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return `inputs` with `NEVER_MODEL_FACING_FIELDS` removed.

    Returns `inputs` itself when nothing changes, so callers can cheaply detect a no-op via `is`.
    """
    if not (NEVER_MODEL_FACING_FIELDS & inputs.keys()):
        return inputs
    return {name: spec for name, spec in inputs.items() if name not in NEVER_MODEL_FACING_FIELDS}


class _AlgentaToolWrapper(Tool):
    """A model-facing smolagents `Tool` that wraps an underlying tool and applies governance.

    The wrapper performs two tasks:

    1. **Never-model-facing scrubbing** -- `force` / `override_safety` are removed from the
       advertised `inputs` schema and stripped from every forwarded argument dict.
    2. **Typed `execute_decision` mapping** -- the raw result is parsed as an `ExecutionReceipt`
       on success or an `ExecutionDenial` on a named policy-gate block. A denial is surfaced as
       an `AgentToolExecutionError` whose message preserves the gate name, engine message, and
       override hint. Every other tool's result passes through unchanged.

    `skip_forward_signature_validation` is set because each wrapped tool has a different input
    signature; the wrapper accepts arbitrary keyword arguments and validates them against the
    scrubbed `inputs` schema at runtime through smolagents' own machinery.
    """

    name: str = ""
    description: str = ""
    inputs: dict[str, dict[str, Any]] = {}
    output_type: str = "object"
    output_schema: dict[str, Any] | None = None
    skip_forward_signature_validation = True

    def __init__(
        self,
        wrapped: Tool,
        *,
        receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    ) -> None:
        self._wrapped = wrapped
        self._receipt_model = receipt_model

        self.name = wrapped.name
        self.description = wrapped.description
        self.inputs = _strip_never_model_facing_inputs(dict(wrapped.inputs))
        self.output_type = wrapped.output_type
        self.output_schema = getattr(wrapped, "output_schema", None)
        self.is_initialized = True

    def forward(self, **kwargs: Any) -> Any:
        scrubbed = {k: v for k, v in kwargs.items() if k not in NEVER_MODEL_FACING_FIELDS}
        raw_result = self._wrapped.forward(**scrubbed)

        denial = parse_denial(raw_result)
        if denial is not None:
            raise AgentToolExecutionError(
                f"execute_decision blocked by '{denial.gate}' gate: {denial.denial_reason()}",
                _ERROR_LOGGER,
            )

        receipt = parse_receipt(raw_result, model=self._receipt_model)
        if receipt is not None:
            return receipt

        return raw_result


class AlgentaToolset:
    """Wraps an MCP tool collection pointed at a self-hosted Algenta engine with governance.

    Layers three things on top of the engine's real MCP tool surface:

    1. **Tool-profile filtering** (`profile=`): only the tool names
       [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
       assigns to the requested profile are exposed to the model. Defaults to `"observe"`
       (read-only), matching the contract's own stated default.
    2. **Typed `execute_decision` results**: `execute_decision`'s raw result is parsed into a
       typed [`ExecutionReceipt`][smolagents_algenta.receipts.ExecutionReceipt] (success) or
       raised as an `AgentToolExecutionError` (a named policy-gate denial) -- see
       `_AlgentaToolWrapper.forward`. Every other tool's result (`plan_decision`,
       `log_decision`, `get_contract`, ...) is freeform and passes through unchanged; this
       package doesn't invent a shared envelope for tools that don't have one.
    3. **Never-model-facing scrubbing**: `force` / `override_safety` are stripped from every
       tool schema and every forwarded argument dict, so no profile can make them model-facing.

    The toolset is a **synchronous context manager** when connecting over MCP:

    ```python {test="skip"}
    from smolagents import CodeAgent
    from smolagents_algenta import AlgentaToolset

    with AlgentaToolset(base_url="http://localhost:8000/mcp", profile="observe") as toolset:
        agent = CodeAgent(tools=[*toolset.tools], model=model)
        agent.run("What should we do about scenario X?")
    ```

    For tests or advanced use, pass an existing sequence of smolagents `Tool` objects via
    `wrapped=`; that path is also fully synchronous and requires no network connection.
    """

    profile: ToolProfile
    receipt_model: type[ExecutionReceipt]
    tools: list[Tool]

    def __init__(
        self,
        *,
        base_url: str | None = None,
        profile: ToolProfile = DEFAULT_PROFILE,
        wrapped: Sequence[Tool] | None = None,
        receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    ) -> None:
        """Build a new `AlgentaToolset`.

        Args:
            base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
                `ALGENTA_BASE_URL` environment variable, falling back to
                `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta
                cloud default). Ignored if `wrapped` is given.
            profile: Which tool profile to expose to the model -- one of `"observe"` (default),
                `"govern"`, `"execute"`, or `"full"`. See
                [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
            wrapped: Advanced escape hatch / test seam: pass an already-built sequence of
                smolagents `Tool` objects instead of connecting to an MCP server. Mutually
                exclusive with `base_url`.
            receipt_model: The `ExecutionReceipt` subclass to validate `execute_decision`'s
                successful result against. Override if your engine's receipt envelope has grown
                fields you want typed (the base model already accepts and preserves unknown
                fields via `extra="allow"`, so most callers won't need this).

        Raises:
            ValueError: If `profile` isn't one of the four contract profiles, or if both
                `wrapped` and `base_url` are given.
        """
        from .contract import TOOL_PROFILES  # local import: avoids a module-level cycle risk

        if profile not in TOOL_PROFILES:
            raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")

        if wrapped is not None and base_url is not None:
            raise ValueError("Pass either `wrapped` or `base_url`, not both.")

        self.profile = profile
        self.receipt_model = receipt_model
        self.tools = []
        self._wrapped_source = wrapped
        self._client: _BackgroundMCPClient | None = None
        self._resolved_base_url: str | None = None

        if wrapped is None:
            self._resolved_base_url = (
                base_url
                or os.environ.get(ALGENTA_BASE_URL_ENV_VAR)
                or DEFAULT_ALGENTA_BASE_URL
            )

    def __enter__(self) -> AlgentaToolset:
        """Connect to the MCP server (or use `wrapped=`), list tools, and expose the governed subset."""
        if self._wrapped_source is not None:
            self.tools = self._wrap_tools(list(self._wrapped_source))
            return self

        self._client = _BackgroundMCPClient(self._resolved_base_url)
        self._client.connect()
        mcp_tools = self._client.list_tools()
        smol_tools = [self._mcp_tool_to_smolagents(t) for t in mcp_tools]
        self.tools = self._wrap_tools(smol_tools)
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the underlying MCP client."""
        if self._client is not None:
            self._client.disconnect()
            self._client = None

    def _wrap_tools(self, tools: list[Tool]) -> list[Tool]:
        """Apply profile filtering and build governed wrappers."""
        allowed_names = resolve_profile_tool_names(
            self.profile,
            available_tool_names=frozenset(t.name for t in tools),
        )
        wrapped: list[Tool] = []
        for tool in tools:
            if tool.name not in allowed_names:
                continue
            if tool.name == "execute_decision":
                wrapped.append(_AlgentaToolWrapper(tool, receipt_model=self.receipt_model))
            else:
                wrapped.append(_AlgentaToolWrapper(tool))
        return wrapped

    def _mcp_tool_to_smolagents(self, mcp_tool: Any) -> Tool:
        """Convert an MCP tool definition into a smolagents `Tool` instance."""
        schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}
        inputs = _mcp_schema_to_inputs(schema)

        # A `Tool` subclass must define `forward` with a matching signature for validation.
        # Because each tool has a different signature, we build the class dynamically with
        # `skip_forward_signature_validation` and accept **kwargs.
        client = self._client
        tool_name = mcp_tool.name

        def forward(self, **kwargs: Any) -> Any:  # noqa: ARG001
            result = client.call_tool(tool_name, kwargs)
            return _extract_tool_result(result)

        dynamic_cls = type(
            f"_DynamicTool_{tool_name}",
            (Tool,),
            {
                "name": tool_name,
                "description": mcp_tool.description or "",
                "inputs": inputs,
                "output_type": "object",
                "output_schema": None,
                "skip_forward_signature_validation": True,
                "forward": forward,
            },
        )
        return dynamic_cls()


def _format_tool_result_content(content: list[Any]) -> str:
    """Build a readable string from a list of MCP content items for error messages."""
    parts: list[str] = []
    for item in content:
        if getattr(item, "type", None) == "text":
            parts.append(getattr(item, "text", str(item)))
        elif hasattr(item, "model_dump"):
            parts.append(str(item.model_dump()))
        else:
            parts.append(str(item))
    return " ".join(parts) if parts else str(content)


def _extract_tool_result(result: Any) -> Any:
    """Extract a JSON-serializable value from a FastMCP `CallToolResult`.

    Falls back through `structured_content`, a single text content item (JSON-parsed if possible),
    and finally the raw content list. This matches what smolagents expects from a tool's
    `forward` method: a plain Python object, not an MCP transport envelope.
    """
    if getattr(result, "is_error", False):
        # The engine signals policy denials via a structured error body, not via
        # CallToolResult.is_error, so a transport-level tool error is surfaced as-is.
        content = getattr(result, "content", result)
        message = _format_tool_result_content(content) if isinstance(content, list) else str(content)
        raise AgentToolExecutionError(
            f"MCP tool returned an error: {message}",
            _ERROR_LOGGER,
        )
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list) and len(content) == 1 and getattr(content[0], "type", None) == "text":
        text = content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if isinstance(content, list):
        return [c.model_dump() if hasattr(c, "model_dump") else c for c in content]
    return result
