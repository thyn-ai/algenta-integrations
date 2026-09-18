"""`ScriptedChatGenerator` -- a scripted, no-network chat generator for exercising the REAL
`haystack.components.agents.Agent` run loop in tests.

Same research technique as `pydantic-ai`'s `TestModel` or LangChain's `FakeListChatModel`: a real
Haystack `@component` implementing the `ChatGenerator` protocol (`run(messages, tools=None,
**kwargs) -> {"replies": [...]}`), with only the "what would the model say next" decision
scripted (a plain queue of pre-built reply lists). No network egress happens at any point, and the
real `Agent` step loop -- `before_tool`/`after_tool` hooks, `ConfirmationHook`'s human-in-the-loop
gate, real tool invocation -- runs exactly as it would with a real chat model.

`haystack-ai` 3.0.0 ships no built-in test double of its own with this exact shape (there is no
`haystack.testing.TestChatGenerator` or similar), which is why this package hand-rolls one, the
same as `maf_algenta`'s `tests/fake_chat_client.py` does for `agent_framework`.
"""

from __future__ import annotations

from typing import Any

from haystack import component
from haystack.dataclasses import ChatMessage, ToolCall


@component
class ScriptedChatGenerator:
    """Returns pre-scripted reply lists from a queue, one per underlying model call."""

    def __init__(self, scripted_replies: list[list[ChatMessage]]) -> None:
        self._queue = list(scripted_replies)
        self.calls: list[list[ChatMessage]] = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages: list[ChatMessage], tools: Any = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(list(messages))
        if not self._queue:
            return {"replies": [ChatMessage.from_assistant(text="(no more scripted replies)")]}
        return {"replies": self._queue.pop(0)}

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tests.fake_chat_generator.ScriptedChatGenerator", "init_parameters": {}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScriptedChatGenerator:
        return cls(scripted_replies=[])


def tool_call_reply(tool_name: str, arguments: dict[str, Any], call_id: str = "call-1") -> list[ChatMessage]:
    """One scripted reply: the model decides to call `tool_name` with `arguments`."""
    return [ChatMessage.from_assistant(text=None, tool_calls=[ToolCall(id=call_id, tool_name=tool_name, arguments=arguments)])]


def text_reply(text: str) -> list[ChatMessage]:
    """One scripted reply: the model gives a plain final text answer."""
    return [ChatMessage.from_assistant(text=text)]


__all__ = ["ScriptedChatGenerator", "text_reply", "tool_call_reply"]
