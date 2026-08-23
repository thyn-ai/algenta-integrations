"""Shared, non-fixture test helpers."""

from __future__ import annotations

from typing import Any

from llama_index.core.base.llms.types import ChatMessage, ChatResponse, LLMMetadata, MessageRole
from llama_index.core.llms.function_calling import FunctionCallingLLM
from llama_index.core.workflow import Context, StartEvent, StopEvent, Workflow, step


class _BareWorkflow(Workflow):
    """A workflow with a trivial, never-invoked step -- exists only so `Context(...)` has
    something to wrap (a real `Workflow` subclass must have at least one `@step` accepting a
    `StartEvent`, or its own constructor rejects it before `Context(...)` is ever reached).

    Never `.run()`, so the `Context` built from it stays in `PreContext` state forever, which is
    exactly what exercises `llamaindex_algenta.exceptions.AlgentaApprovalStillPending`'s
    `ContextStateError` fallback path: `ctx.wait_for_event(...)` on a `PreContext` raises
    `workflows.errors.ContextStateError("... requires a running workflow. Call workflow.run() first.")`.
    """

    @step
    async def _never_runs(self, ev: StartEvent) -> StopEvent:
        return StopEvent(result=None)


def bare_context() -> Context:
    """A minimal, standalone `Context` for calling a wrapped `FunctionTool.acall(ctx=..., ...)`
    directly, without spinning up a full agent/workflow run.

    Fine for the toolset-level unit tests in this suite that exercise a *successful*,
    *denied*, or *failed* governed call (none of those ever touch `ctx.wait_for_event()`) -- and
    is the deliberate fixture for proving the pending-approval fail-closed fallback (there is no
    live step here for `wait_for_event()` to actually pause).
    """
    return Context(_BareWorkflow())


class ScriptedFunctionCallingLLM(FunctionCallingLLM):
    """A deterministic, scripted `FunctionCallingLLM` stand-in -- no network calls, fully
    reproducible. Each entry in `turns` is either a `list[ToolSelection]` (call these tools this
    turn) or a `str` (finish the run with this content).

    Mirrors this package's own research probes (`probe_hitl2.py`/`probe_hitl3.py`) that first
    proved `FunctionAgent.run()` genuinely pauses mid-tool-call via `Context.wait_for_event()`,
    generalized here into one reusable scriptable stand-in for the whole test suite.
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
