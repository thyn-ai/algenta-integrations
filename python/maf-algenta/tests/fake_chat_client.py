"""`FakeChatClient` -- a scripted, no-network chat client for exercising the REAL
`agent_framework.Agent` run loop in tests.

Same research technique as pydantic-ai's `TestModel` / LangChain's `FakeListChatModel`: a real
`agent_framework._clients.BaseChatClient` subclass composed with the REAL
`FunctionInvocationLayer` / `ChatMiddlewareLayer` / `ChatTelemetryLayer` mixins -- the exact same
MRO shape `agent_framework_openai.OpenAIChatClient` uses in production, confirmed by inspecting
that class's `__mro__` directly while researching this package -- so the REAL approval-gate /
function-invocation loop runs end to end. Only the "what would the model say next" decision is
scripted (a plain queue of pre-built `ChatResponse`s), and no network egress happens at any point.

`agent_framework` 1.15.0 ships no built-in test double of its own (`agent_framework.testing`, or
anything with "test"/"fake"/"mock" in its name, does not exist in the installed package --
checked directly), unlike pydantic-ai and LangChain, which is why this package hand-rolls one
rather than importing something upstream.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework import ChatResponse, Content, Message
from agent_framework._clients import BaseChatClient
from agent_framework._middleware import ChatMiddlewareLayer
from agent_framework._tools import FunctionInvocationLayer
from agent_framework.observability import ChatTelemetryLayer


class FakeChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, ChatTelemetryLayer, BaseChatClient):
    """Returns pre-scripted `ChatResponse`s from a queue, one per underlying model call."""

    OTEL_PROVIDER_NAME = "fake"

    def __init__(self, scripted_responses: list[ChatResponse], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._queue = list(scripted_responses)
        self.calls: list[list[Message]] = []

    async def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ChatResponse:
        self.calls.append(list(messages))
        if not self._queue:
            return ChatResponse(
                messages=Message(role="assistant", contents=[Content.from_text("(no more scripted responses)")])
            )
        return self._queue.pop(0)


def function_call(name: str, arguments: str, call_id: str) -> Content:
    """Build a `Content(type="function_call")` for a scripted `ChatResponse` -- the shape a real
    model's response takes when it decides to call a tool."""
    return Content.from_function_call(call_id=call_id, name=name, arguments=arguments)


__all__ = ["FakeChatClient", "function_call"]
