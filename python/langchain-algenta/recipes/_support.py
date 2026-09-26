"""Shared helpers for the recipes: a scripted chat model, tool lookup, payload unwrapping.

`ScriptedChatModel` is what lets every recipe run a *real* LangChain agent loop -- real
`langchain.agents.create_agent`, real LangGraph model/tool nodes, real tool execution over
the real MCP wire against the stub engine -- with no LLM API key. It replays a fixed
script of `AIMessage`s (including their `tool_calls`), so the orchestration under test is
entirely real; only the model's weights are stubbed. Swap in any real chat model
(`init_chat_model("openai:gpt-5")`, etc.) to take a recipe to production.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool


class ScriptedChatModel(BaseChatModel):
    """A deterministic, credential-free chat model that replays a fixed script.

    Each LLM call returns the next `AIMessage` in `script` (repeating the last one if the
    loop runs longer than the script -- a script should end with a tool-call-free final
    answer, so an agent loop always terminates). `bind_tools` is a no-op returning `self`:
    the script already carries exactly the `tool_calls` the recipe intends the model to
    make, in order.
    """

    script: list[AIMessage]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-chat-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


def tool_by_name(tools: list[BaseTool], name: str) -> BaseTool:
    """Return the one tool named `name` from a tool list, or fail loudly with what exists."""
    for tool in tools:
        if tool.name == name:
            return tool
    available = sorted(tool.name for tool in tools)
    raise KeyError(f"No tool named {name!r}; available tools: {available}")


def first_text_json(raw_result: Any) -> Any:
    """Unwrap the JSON payload from an MCP tool call's LangChain content-block result.

    A `create_algenta_tools` tool's `ainvoke(...)` returns a list of LangChain content
    blocks; the tool's JSON payload is the first `text` block's text -- the same shape a
    real chat model would see in the corresponding `ToolMessage`.
    """
    if isinstance(raw_result, list):
        for block in raw_result:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block["text"])
    raise AssertionError(f"expected a list of text content blocks, got {raw_result!r}")


def tool_message_payloads(messages: list[BaseMessage], *, name: str) -> list[Any]:
    """Extract the parsed JSON payloads of every `ToolMessage` a given tool produced."""
    from langchain_core.messages import ToolMessage

    payloads = []
    for message in messages:
        if isinstance(message, ToolMessage) and message.name == name:
            content = message.content
            if isinstance(content, list):
                payloads.append(first_text_json(content))
            else:
                payloads.append(json.loads(content))
    return payloads
