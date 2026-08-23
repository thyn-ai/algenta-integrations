"""Shared, non-fixture test helpers."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from langgraph.graph.message import MessagesState


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


def build_single_tool_call_graph(tool: BaseTool, args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Build a tiny compiled LangGraph graph with exactly one node that calls `tool.ainvoke(args)`.

    Used to exercise `AlgentaToolCallInterceptor`'s `langgraph.types.interrupt(...)` pause/resume
    behavior for real, inside a genuine Pregel task context with a real checkpointer -- not just
    asserting that `interrupt()` was *called*. `interrupt()` itself raises a plain `RuntimeError`
    ("Called get_config outside of a runnable context") when there's no such context at all --
    verified directly against the installed `langgraph` package -- so a pending-approval scenario
    can only be exercised meaningfully via a real compiled graph like this one, not by calling
    `tool.ainvoke(...)` bare (see `tests/test_approval_mapping.py`'s
    `test_pending_approval_outside_any_graph_context_raises_runtime_error` for that contrasting
    case, which is itself part of this package's documented, honest behavior).

    Returns `(graph, config)`; `config`'s `thread_id` is a fresh UUID, stable across the initial
    `ainvoke` and any later `Command(resume=...)` call needed to resume the same paused run.
    """

    async def call_tool_node(state: MessagesState) -> dict[str, Any]:
        result = await tool.ainvoke(args)
        return {"messages": [AIMessage(content=repr(result))]}

    builder = StateGraph(MessagesState)
    builder.add_node("call_tool", call_tool_node)
    builder.set_entry_point("call_tool")
    builder.set_finish_point("call_tool")
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    return graph, config
