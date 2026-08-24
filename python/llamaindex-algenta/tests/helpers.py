"""Shared, non-fixture test helpers."""

from __future__ import annotations

from typing import Any

from llama_index.core.base.llms.types import ChatMessage, ChatResponse, LLMMetadata, MessageRole
from llama_index.core.llms.function_calling import FunctionCallingLLM


class ScriptedFunctionCallingLLM(FunctionCallingLLM):
    """A deterministic, scripted `FunctionCallingLLM` stand-in -- no network calls, fully
    reproducible. Each entry in `turns` is either a `list[ToolSelection]` (call these tools this
    turn) or a `str` (finish the run with this content).
    """

    _turns: list[Any] = []
    _index: int = 0

    def __init__(self, turns: list[Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns = list(turns)
        self._index = 0

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(is_function_calling_model=True, is_chat_model=True)

    def _prepare_chat_with_tools(
        self,
        tools: Any,
        user_msg: Any = None,
        chat_history: Any = None,
        verbose: bool = False,
        allow_parallel_tool_calls: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"messages": chat_history or []}

    def get_tool_calls_from_response(self, response: Any, error_on_no_tool_call: bool = True, **kwargs: Any) -> list[Any]:
        return response.message.additional_kwargs.get("tool_selections", [])

    async def achat(self, messages: Any, **kwargs: Any) -> ChatResponse:
        turn = self._turns[self._index]
        self._index += 1
        if isinstance(turn, str):
            return ChatResponse(message=ChatMessage(role=MessageRole.ASSISTANT, content=turn))
        return ChatResponse(
            message=ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                additional_kwargs={"tool_selections": list(turn)},
            )
        )

    # Unused abstract surface -- implemented minimally to satisfy FunctionCallingLLM's ABC.
    def chat(self, messages: Any, **kwargs: Any) -> ChatResponse:
        raise NotImplementedError

    def stream_chat(self, messages: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def astream_chat(self, messages: Any, **kwargs: Any) -> Any:
        response = await self.achat(messages, **kwargs)

        async def _gen() -> Any:
            yield response

        return _gen()

    def complete(self, prompt: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def acomplete(self, prompt: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def stream_complete(self, prompt: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def astream_complete(self, prompt: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
