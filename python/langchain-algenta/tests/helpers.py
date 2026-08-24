"""Shared, non-fixture test helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool


def mcp_shaped_tool(
    name: str,
    *,
    description: str = "",
    schema: dict[str, Any],
    coroutine: Callable[..., Awaitable[Any]],
) -> BaseTool:
    """Build a `BaseTool` with a raw JSON-Schema `dict` `args_schema` -- the exact shape
    `langchain_mcp_adapters.tools.convert_mcp_tool_to_langchain_tool` gives every real MCP tool
    (`StructuredTool(args_schema=tool.inputSchema, ...)`, verified directly against the
    installed library), unlike the pydantic-model-class schema the `@tool` decorator or
    `StructuredTool.from_function(infer_schema=True)` would produce from a plain function
    signature. `create_algenta_tools(tools=...)`'s never-model-facing schema scrub
    (`_strip_never_model_facing_schema`) only handles the `dict` shape, by design -- it's the
    only shape this package's real MCP path ever actually produces -- so tests exercising that
    scrub need fake tools built this way, not via `@tool`.
    """
    return StructuredTool(name=name, description=description, args_schema=schema, coroutine=coroutine)
