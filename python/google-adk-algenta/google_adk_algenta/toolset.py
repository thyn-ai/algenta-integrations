"""`AlgentaToolset` -- a governed-execution-aware ADK `McpToolset` wrapper.

Layers three things on top of the wrapped MCP tools' real calls:

1. **Tool-profile filtering** (`profile=`): only the tool names
   [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
   assigns to the requested profile are exposed to the model. Defaults to `"observe"`
   (read-only), matching the contract's own stated default.
2. **Never-model-facing scrubbing**: `force`/`override_safety` are stripped from every
   returned tool's advertised JSON schema (`get_tools`) *and* from the arguments dict
   actually forwarded to the wrapped MCP call (`run_async`).
3. **Typed `execute_decision` results**: a successful result validates as an
   [`ExecutionReceipt`][google_adk_algenta.receipts.ExecutionReceipt] and is returned as a
   typed object; a synchronous policy-gate denial raises
   [`AlgentaExecutionBlocked`][google_adk_algenta.exceptions.AlgentaExecutionBlocked] with the
   real gate name and the engine's own message/override hint attached.

`execute_decision` is fully synchronous: a call either succeeds or is blocked in the same call
by exactly one of three named policy gates. There is no asynchronous "pending approval" state,
so this package does not model one.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.adk.tools import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.adk.tools.mcp_tool.mcp_tool import McpTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai.types import FunctionDeclaration

from .contract import (
    DEFAULT_PROFILE,
    NEVER_MODEL_FACING_FIELDS,
    ToolProfile,
    is_tool_allowed_for_profile,
    resolve_profile_tool_names,
)
from .exceptions import AlgentaExecutionBlocked, AlgentaToolDenied
from .receipts import ExecutionReceipt, parse_denial, parse_receipt

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=`
#: constructor argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"


def _strip_never_model_facing_schema(schema: Any) -> Any:
    """Return `schema` with `NEVER_MODEL_FACING_FIELDS` removed from `properties`/`required`.

    Returns `schema` itself, unchanged, when it isn't a `dict` or none of those fields are
    present -- so callers can cheaply tell via `is` whether anything actually changed.
    """
    if not isinstance(schema, dict):
        return schema
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not (NEVER_MODEL_FACING_FIELDS & properties.keys()):
        return schema
    new_schema = dict(schema)
    new_schema["properties"] = {
        k: v for k, v in properties.items() if k not in NEVER_MODEL_FACING_FIELDS
    }
    required = schema.get("required")
    if isinstance(required, list):
        new_schema["required"] = [
            name for name in required if name not in NEVER_MODEL_FACING_FIELDS
        ]
    return new_schema


def _strip_function_declaration(decl: FunctionDeclaration) -> FunctionDeclaration:
    """Return `decl` with never-model-facing fields stripped from its JSON schema.

    Returns `decl` itself, unchanged, when no stripping was needed.
    """
    schema = decl.parameters_json_schema
    new_schema = _strip_never_model_facing_schema(schema)
    if new_schema is schema:
        return decl
    new_decl = decl.model_copy(deep=True)
    new_decl.parameters_json_schema = new_schema
    return new_decl


def _extract_tool_payload(raw_result: Any) -> Any:
    """Extract the actual tool payload from an ADK/MCP tool result.

    ADK's `McpTool.run_async` returns the dumped MCP `CallToolResult` dict. On mcp 1.x that
    dict carries `structuredContent` for schema-shaped results and `content` (a list of text
    blocks) for plain returns. This helper prefers `structuredContent`, falls back to parsing
    the first text block as JSON, and returns the input unchanged if neither yields a payload.
    """
    if not isinstance(raw_result, dict):
        return raw_result
    structured = raw_result.get("structuredContent")
    if structured is not None:
        return structured
    for block in raw_result.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    return raw_result


def _map_execute_decision(
    tool_name: str,
    raw_result: Any,
    *,
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
) -> Any:
    """Map an `execute_decision` result onto a typed receipt or a named denial exception.

    - A successful result that validates as an `ExecutionReceipt` is returned as the typed
      object.
    - A named policy-gate denial raises `AlgentaExecutionBlocked`.
    - Any other result passes through unchanged.
    """
    payload = _extract_tool_payload(raw_result)

    denial = parse_denial(payload)
    if denial is not None:
        raise AlgentaExecutionBlocked(
            f"Algenta tool {tool_name!r} was blocked by the {denial.gate!r} policy gate -- {denial.message}",
            denial=denial,
        )

    receipt = parse_receipt(payload, model=receipt_model)
    if receipt is not None:
        return receipt

    return raw_result


class AlgentaMcpTool(McpTool):
    """An ADK `McpTool` with Algenta profile enforcement and `execute_decision` mapping."""

    def __init__(
        self,
        *,
        profile: ToolProfile,
        receipt_model: type[ExecutionReceipt],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._algenta_profile = profile
        self._algenta_receipt_model = receipt_model

    def _get_declaration(self) -> FunctionDeclaration:
        """Return the tool declaration with never-model-facing fields scrubbed from the schema."""
        return _strip_function_declaration(super()._get_declaration())

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        """Run the tool with profile check, argument scrubbing, and receipt/denial mapping."""
        if not is_tool_allowed_for_profile(self.name, self._algenta_profile):
            raise AlgentaToolDenied(
                f"Algenta tool {self.name!r} is not part of the {self._algenta_profile!r} profile; "
                "refusing to call it."
            )

        scrubbed_args = {k: v for k, v in args.items() if k not in NEVER_MODEL_FACING_FIELDS}
        raw_result = await super().run_async(args=scrubbed_args, tool_context=tool_context)
        return _map_execute_decision(
            self.name, raw_result, receipt_model=self._algenta_receipt_model
        )


class AlgentaBaseTool(BaseTool):
    """Wraps a plain ADK `BaseTool` (e.g. a `FunctionTool`) with the same Algenta layers.

    Used by the `tools=` escape hatch so in-memory/test tools get the same profile,
    scrubbing, and `execute_decision` mapping as real MCP tools.
    """

    def __init__(
        self,
        *,
        tool: BaseTool,
        profile: ToolProfile,
        receipt_model: type[ExecutionReceipt],
    ) -> None:
        super().__init__(name=tool.name, description=tool.description)
        self._tool = tool
        self._algenta_profile = profile
        self._algenta_receipt_model = receipt_model

    def _get_declaration(self) -> Any:
        """Return the wrapped tool's declaration with never-model-facing fields scrubbed."""
        decl = self._tool._get_declaration()
        if decl is None:
            return None
        return _strip_function_declaration(decl)

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        """Run the wrapped tool with profile check, argument scrubbing, and mapping."""
        if not is_tool_allowed_for_profile(self.name, self._algenta_profile):
            raise AlgentaToolDenied(
                f"Algenta tool {self.name!r} is not part of the {self._algenta_profile!r} profile; "
                "refusing to call it."
            )

        scrubbed_args = {k: v for k, v in args.items() if k not in NEVER_MODEL_FACING_FIELDS}
        raw_result = await self._tool.run_async(args=scrubbed_args, tool_context=tool_context)
        return _map_execute_decision(
            self.name, raw_result, receipt_model=self._algenta_receipt_model
        )


class _StaticToolset(BaseToolset):
    """A simple toolset that returns a fixed list of tools (used by the `tools=` escape hatch)."""

    def __init__(self, tools: list[BaseTool]) -> None:
        super().__init__()
        self._tools = list(tools)

    async def get_tools(self, readonly_context: Any = None) -> list[BaseTool]:
        return list(self._tools)


class AlgentaToolset(BaseToolset):
    """Wraps an ADK `McpToolset` (or a list of test tools) with governed-execution awareness.

    Constructs its own inner `McpToolset` -- you don't build one yourself and hand it over
    (pass `base_url=` to configure it instead) -- and layers three things on top of the wrapped
    toolset's real MCP tool calls:

    1. **Tool-profile filtering** (`profile=`): only the tool names
       [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
       assigns to the requested profile are exposed to the model. Defaults to `"observe"`
       (read-only), matching the contract's own stated default.
    2. **Typed `execute_decision` results**: a successful result validates as an
       [`ExecutionReceipt`][google_adk_algenta.receipts.ExecutionReceipt] and is returned as a
       typed object; a synchronous policy-gate denial raises
       [`AlgentaExecutionBlocked`][google_adk_algenta.exceptions.AlgentaExecutionBlocked].
    3. **Never-model-facing scrubbing**: `force`/`override_safety` are stripped from the
       advertised JSON schema *and* from the arguments dict actually forwarded to the wrapped
       call.

    Example:

    ```python {test="skip"}
    from google.adk.agents import LlmAgent
    from google_adk_algenta import AlgentaToolset

    toolset = AlgentaToolset(base_url="http://localhost:8000/mcp", profile="observe")
    agent = LlmAgent(name="algenta_agent", model="gemini-2.5-pro", tools=[toolset])
    ```
    """

    profile: ToolProfile
    receipt_model: type[ExecutionReceipt]

    def __init__(
        self,
        *,
        base_url: str | None = None,
        profile: ToolProfile = DEFAULT_PROFILE,
        receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
        wrapped: BaseToolset | None = None,
        tools: list[BaseTool] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
        sse_read_timeout: float = 300.0,
    ) -> None:
        """Build a new `AlgentaToolset`.

        Args:
            base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
                `ALGENTA_BASE_URL` environment variable, falling back to
                `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta
                cloud default). Ignored if `wrapped` or `tools` is given.
            profile: Which tool profile to expose to the model -- one of `"observe"` (default),
                `"govern"`, `"execute"`, or `"full"`. See
                [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
            receipt_model: The `ExecutionReceipt` subclass to validate `execute_decision`'s
                successful result against. Override if your engine's receipt envelope has grown
                fields you want typed (the base model already accepts and preserves unknown
                fields via `extra="allow"`, so most callers won't need this).
            wrapped: Advanced escape hatch / test seam: pass an already-built `BaseToolset`
                (typically an ADK `McpToolset`) instead of having `AlgentaToolset` build one from
                `base_url`. Mutually exclusive with `base_url` and `tools`.
            tools: Advanced escape hatch / test seam: pass a list of already-built `BaseTool`s
                to filter and wrap instead of connecting over MCP at all. Mutually exclusive
                with `base_url` and `wrapped`.
            headers: Extra HTTP headers for the self-hosted endpoint (e.g. a static bearer
                token). Forwarded to the constructed `McpToolset`.
            timeout: Connection timeout in seconds for the MCP HTTP connection.
            sse_read_timeout: SSE read timeout in seconds for the MCP HTTP connection.

        Raises:
            ValueError: If `profile` isn't one of the four contract profiles, or if more than
                one of `base_url`, `wrapped`, and `tools` is given.
        """
        from .contract import TOOL_PROFILES  # local import: avoids a module-level cycle risk

        if profile not in TOOL_PROFILES:
            raise ValueError(
                f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}."
            )

        sources_given = sum(x is not None for x in (base_url, wrapped, tools))
        if sources_given > 1:
            raise ValueError(
                "Pass exactly one of `base_url`, `wrapped`, or `tools`, not more than one."
            )

        self.profile = profile
        self.receipt_model = receipt_model

        if wrapped is not None:
            self._inner = wrapped
        elif tools is not None:
            self._inner = _StaticToolset(tools)
        else:
            resolved_base_url = (
                base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
            )
            connection_params = StreamableHTTPConnectionParams(
                url=resolved_base_url,
                headers=headers,
                timeout=timeout,
                sse_read_timeout=sse_read_timeout,
            )
            self._inner = McpToolset(connection_params=connection_params)

    async def get_tools(self, readonly_context: Any = None) -> list[BaseTool]:
        """Return tools filtered to `self.profile` and wrapped with schema/call-time enforcement."""
        tools = await self._inner.get_tools(readonly_context)
        available_names = frozenset(tool.name for tool in tools)
        allowed_names = resolve_profile_tool_names(
            self.profile, available_tool_names=available_names
        )

        wrapped: list[BaseTool] = []
        for tool in tools:
            if tool.name not in allowed_names:
                continue
            wrapped.append(_wrap_tool(tool, profile=self.profile, receipt_model=self.receipt_model))
        return wrapped

    async def close(self) -> None:
        """Release resources held by the inner toolset."""
        await self._inner.close()


def _wrap_tool(
    tool: BaseTool,
    *,
    profile: ToolProfile,
    receipt_model: type[ExecutionReceipt],
) -> BaseTool:
    """Wrap a single tool with Algenta profile enforcement and `execute_decision` mapping."""
    if isinstance(tool, McpTool):
        return AlgentaMcpTool(
            profile=profile,
            receipt_model=receipt_model,
            mcp_tool=tool.raw_mcp_tool,
            mcp_session_manager=tool._mcp_session_manager,
            auth_scheme=getattr(tool, "_auth_scheme", None),
            auth_credential=getattr(tool, "_auth_credential", None),
            require_confirmation=getattr(tool, "_require_confirmation", False),
            header_provider=getattr(tool, "_header_provider", None),
            progress_callback=getattr(tool, "_progress_callback", None),
        )
    return AlgentaBaseTool(tool=tool, profile=profile, receipt_model=receipt_model)


__all__ = [
    "AlgentaToolset",
    "DEFAULT_ALGENTA_BASE_URL",
    "ALGENTA_BASE_URL_ENV_VAR",
]
