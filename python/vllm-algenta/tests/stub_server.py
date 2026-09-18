"""A minimal, deterministic, real stub of Algenta's actual `/v1/chat/completions` and
`/v1/responses` behavior -- a real FastAPI app on a real HTTP socket, never a mock.

Every request/response field below reproduces BY HAND the publicly-observable wire shape of the
engine's public OpenAI-compatible HTTP API (what the real endpoints accept and return over HTTP)
-- never imported or copied from engine source, which `scripts/check-no-engine-dependency.py`
forbids this repository from doing regardless. Reproducing the publicly-observable
request/response SHAPE by hand, with no engine source or logic inside it, is the same pattern
`contracts/integration-tool-contract.json`'s own `provenance_note` documents for this
repository's MCP tool contract. Re-verify against the live engine's public API before trusting
this file blindly if it's been a while.

What this stub reproduces on the `/v1/chat/completions` surface, corrected against the live
public API after an earlier version of this file went stale (tool calling and a widened
`finish_reason` are real on the current engine -- see this file's own `ChatCompletionsRequest`
`tools` / `tool_choice` / `parallel_tool_calls` fields and `ChatCompletionChoice.finish_reason:
Literal["stop", "tool_calls", "length", "content_filter"]` below, which mirror that API):

- Tool calling is real, but MODEL-DEPENDENT, exactly like the real engine's public endpoint:
  - `model="text.tokenizer"` (this package's own default, the zero-config deterministic utility
    model) does not support tool calling. Sending `tools=` against it is REJECTED with a loud
    `422 model_capability_unsupported` error -- not silently dropped. This is itself a real,
    positive change from an earlier engine version this package's stub used to mirror: a caller
    who accidentally sends `tools=` against the default model now finds out immediately, from the
    server, instead of getting a confusing false-positive 200. See
    `test_tools_argument_against_the_default_model_is_rejected_not_silently_ignored`.
  - Any other `model=` in this stub (standing in for a configured provider-backed model, or the
    bundled `algenta_local` Apple-Silicon backend -- both real, both deployment-specific, so this
    stub uses one clearly-synthetic placeholder id, `TOOL_CALLING_MODEL`, rather than guessing a
    real provider's model name) DOES support tool calling: a request with `tools=` gets back a
    genuine `tool_calls` reply and `finish_reason="tool_calls"`. See
    `test_tool_calling_produces_real_tool_calls_and_finish_reason_on_a_tool_capable_model`.
  - Streaming a completion that actually calls a tool is refused with the same
    `422 model_capability_unsupported` the real engine returns (`required_capability:
    "streaming_tool_calls"`) -- the engine's real per-backend streaming and its non-streaming
    tool calling shipped separately, and combining them is still a documented gap on the real
    engine, not something this stub pretends is solved. See
    `test_streaming_is_refused_when_a_tool_call_would_actually_fire`.
- `ChatCompletionChoice.finish_reason` is no longer hardcoded across the whole endpoint -- the
  schema's `Literal` widened to `"stop" | "tool_calls" | "length" | "content_filter"`. This stub
  only ever produces `"stop"` (the deterministic default model) or `"tool_calls"` (the tool-capable
  stand-in model): it does not attempt to simulate `"length"` (token-budget truncation) or
  `"content_filter"`, since both are real per-backend behaviors this stub has no backend to
  reproduce faithfully -- left as an honest gap rather than a fabricated one.
- Non-tool-calling streaming is still post-hoc rechunking of an already-fully-computed string
  (`_stream_text_chunks`, `chunk_size=24`), matching the real endpoint's own fixed-size slicing
  for the deterministic default model. The real engine also has a real, incremental "passthrough"
  streaming mode for models that opt into it; this stub does not simulate that mode, since doing
  so faithfully would require a real upstream backend to stream from -- see this package's README
  for the accurate, model-dependent framing.
- `/v1/responses` streaming still emits exactly the three event types this stub has always
  produced -- `response.created` / `response.output_item.done` / `response.completed`. NOTE: the
  real engine's `/v1/responses` surface has since grown its OWN tool-calling, typed input array,
  and `previous_response_id` support (a separate, larger change from the Chat Completions fix
  this file just received) that this stub does not yet reflect -- see this package's README for
  the honest caveat and this repository's tracked follow-up.

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------------------------
# Request/response models -- reproduced field-for-field from the publicly-observable wire shape
# of the engine's public `/v1` endpoints. Deliberately excludes every field belonging to
# unrelated routes (tokenize, embeddings, rerank, ...) that this package's own README makes no
# claim about.
# ---------------------------------------------------------------------------------------------

#: The real engine's zero-config default utility model -- deterministic, no tool calling, no
#: generation to truncate or interrupt. `finish_reason` is always "stop" for this model, and a
#: `tools=` argument against it is a 422, not a silent no-op. Matches the default the real
#: endpoint applies when no `model` is supplied.
DEFAULT_MODEL = "text.tokenizer"

#: NOT a real Algenta model id -- a clearly-synthetic stand-in this stub uses to represent "any
#: configured provider-backed model, or the bundled algenta_local backend, that supports real tool
#: calling." Which model ids actually have that property is deployment-specific (whatever your
#: own org has configured), so this stub cannot reproduce a real one; it uses this name instead of
#: guessing so nobody mistakes it for a real, universally-available model id.
TOOL_CALLING_MODEL = "stub.tool-calling-demo-model"


class ChatCompletionToolCallFunction(BaseModel):
    name: str
    arguments: str = Field(
        ...,
        description="JSON-encoded arguments for the function call (a string, per the OpenAI wire shape).",
    )


class ChatCompletionToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: ChatCompletionToolCallFunction


class ChatCompletionInputMessage(BaseModel):
    role: Literal["system", "user", "assistant", "developer", "tool"]
    content: str | None = None
    tool_calls: list[ChatCompletionToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatCompletionToolFunctionDef(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None


class ChatCompletionToolDef(BaseModel):
    type: Literal["function"] = "function"
    function: ChatCompletionToolFunctionDef


class ChatCompletionToolChoiceFunctionName(BaseModel):
    name: str


class ChatCompletionNamedToolChoice(BaseModel):
    type: Literal["function"] = "function"
    function: ChatCompletionToolChoiceFunctionName


class ChatCompletionsRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[ChatCompletionInputMessage] = Field(..., min_length=1)
    stream: bool = False
    tools: list[ChatCompletionToolDef] | None = None
    tool_choice: Literal["auto", "none", "required"] | ChatCompletionNamedToolChoice | None = None
    parallel_tool_calls: bool | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0)
    top_p: float | None = Field(default=None, gt=0.0, le=1.0)
    seed: int | None = None


class ChatCompletionOutputMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ChatCompletionToolCall] | None = None


class ChatCompletionChoice(BaseModel):
    index: int
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] = "stop"
    message: ChatCompletionOutputMessage


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ProviderAttemptResponse(BaseModel):
    provider_backend: str
    provider_model_id: str
    outcome: Literal["selected", "failed"]
    error_code: str | None = None


class ChatCompletionsResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    provider_backend: str | None = None
    provider_model_id: str | None = None
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    # No `created` field -- matching the real response exactly (verified separately: the
    # standard `openai` Python client parses this response shape without error regardless,
    # leaving its own `.created` attribute `None` -- see this package's README).


class ResponsesRequest(BaseModel):
    model: str = DEFAULT_MODEL
    input: str | list[str]
    dimensions: int = Field(default=64, gt=0, le=4096)
    stream: bool = False
    # NOTE: the real ResponsesRequest has since grown `tools`, `tool_choice`,
    # `parallel_tool_calls`, `previous_response_id`, and `execute_capability_tools` fields, and
    # `input` now also accepts a typed OpenResponses-style item array in addition to `str |
    # list[str]`. This stub has not been updated for that yet -- see this file's module docstring
    # and this package's README.


class ResponseOutputContent(BaseModel):
    type: Literal["tokenization", "embedding", "text"]
    text: str
    tokens: list[str] | None = None
    token_count: int
    embedding: list[float] | None = None


class ResponseOutputItem(BaseModel):
    id: str
    object: Literal["response.output"] = "response.output"
    index: int
    provider_backend: str | None = None
    provider_model_id: str | None = None
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    content: list[ResponseOutputContent]


class EmbeddingUsageResponse(BaseModel):
    prompt_tokens: int
    total_tokens: int


class ResponsesResponse(BaseModel):
    id: str
    object: Literal["response"] = "response"
    status: Literal["completed"] = "completed"
    model: str
    provider_backend: str | None = None
    provider_model_id: str | None = None
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    output: list[ResponseOutputItem]
    usage: EmbeddingUsageResponse


# ---------------------------------------------------------------------------------------------
# Deterministic "completion" logic -- NOT a reimplementation of the real engine's tokenizer or
# any model. This stub's whole job is to reproduce the real *shape* (schema, finish_reason
# values, tool-calling gating, rechunked-streaming mechanics) faithfully; the actual text/argument
# content it returns is a simple, deterministic function of the input, clearly synthetic, never
# claimed as anything else.
# ---------------------------------------------------------------------------------------------

_STREAM_CHUNK_SIZE = 24  # matches the real endpoint's own fixed-size slicing exactly


def _stream_text_chunks(text: str, *, chunk_size: int = _STREAM_CHUNK_SIZE) -> list[str]:
    """Mirrors the real endpoint's observable chunking exactly -- post-hoc slicing of an
    already-fully-computed string, not incremental generation."""
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _deterministic_reply(messages: list[ChatCompletionInputMessage]) -> str:
    last = messages[-1]
    if last.role == "tool":
        return f"stub reply using tool result: {last.content}"
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), "") or ""
    return f"stub reply to: {last_user}"


def _named_tool_choice_function_name(
    tool_choice: Literal["auto", "none", "required"] | ChatCompletionNamedToolChoice | None,
) -> str | None:
    if isinstance(tool_choice, ChatCompletionNamedToolChoice):
        return tool_choice.function.name
    return None


def _should_call_a_tool(payload: ChatCompletionsRequest) -> bool:
    """Mirrors the real engine's own gating for the tool-capable stand-in model: a tool fires
    on this turn only when tools were offered, the caller didn't set tool_choice="none", and this
    turn is a fresh user turn rather than the caller already answering a previous tool call
    (an assistant would not call the same tool again immediately after receiving its result)."""
    if not payload.tools:
        return False
    if payload.tool_choice == "none":
        return False
    if payload.messages[-1].role == "tool":
        return False
    return True


def _tool_call_response(payload: ChatCompletionsRequest) -> ChatCompletionToolCall:
    assert payload.tools
    name = _named_tool_choice_function_name(payload.tool_choice) or payload.tools[0].function.name
    return ChatCompletionToolCall(
        id=f"call_{uuid.uuid4().hex[:12]}",
        function=ChatCompletionToolCallFunction(
            name=name,
            # A real backend extracts real arguments from the conversation; this stub is
            # deterministic and does not attempt that -- it always returns an empty JSON object.
            arguments="{}",
        ),
    )


def _model_capability_unsupported(*, model: str, required_capability: str, message: str) -> HTTPException:
    """Same error shape the real endpoint produces for a capability the model doesn't have:
    HTTP 422, top-level `{"error": {...}}` (no FastAPI `{"detail": ...}` wrapper) -- see this
    stub's own `_http_exception_handler` below, which mirrors the real app's envelope handling."""
    return HTTPException(
        status_code=422,
        detail={
            "error": {
                "code": "model_capability_unsupported",
                "message": message,
                "details": {"model": model, "required_capability": required_capability},
                "request_id": None,
            }
        },
    )


def _sse(payload: Any) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def build_stub_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        # Mirrors the real endpoint's error envelope: an `{"error": {...}}`-shaped detail is
        # returned as the top-level body, unwrapped from FastAPI's default `{"detail": ...}`
        # envelope, exactly like the real app.
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: ChatCompletionsRequest):
        if payload.tools and payload.model == DEFAULT_MODEL:
            # Matches the real endpoint's behavior exactly: the deterministic default model has
            # no tool-calling mechanism at all, so a tools= argument against it is REJECTED
            # (422), not silently dropped.
            raise _model_capability_unsupported(
                model=payload.model,
                required_capability="tool_calling",
                message=f"Model '{payload.model}' does not support tool calling.",
            )

        calls_a_tool = _should_call_a_tool(payload)

        if payload.stream and calls_a_tool:
            # Matches the real engine's own refusal: real per-backend streaming and
            # non-streaming tool calling shipped separately, and a completion that actually
            # calls a tool cannot yet be streamed -- refuse loudly instead of dropping the
            # tool_calls.
            raise _model_capability_unsupported(
                model=payload.model,
                required_capability="streaming_tool_calls",
                message="Streaming is not yet supported for a completion that calls tools; set stream=false.",
            )

        completion_id = f"cmpl-{uuid.uuid4().hex[:12]}"
        prompt_tokens = sum(len((m.content or "").split()) for m in payload.messages)

        if calls_a_tool:
            tool_call = _tool_call_response(payload)
            content: str | None = None
            finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] = "tool_calls"
            tool_calls: list[ChatCompletionToolCall] | None = [tool_call]
            completion_tokens = len(tool_call.function.name.split())
        else:
            content = _deterministic_reply(payload.messages)
            finish_reason = "stop"
            tool_calls = None
            completion_tokens = len(content.split())

        if payload.stream:
            # Only reachable here when calls_a_tool is False -- the calls_a_tool+stream case was
            # already refused above, matching the real engine.
            async def gen():
                yield _sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "model": payload.model,
                        "provider_backend": None,
                        "provider_model_id": None,
                        "provider_attempts": [],
                        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    }
                )
                for chunk in _stream_text_chunks(content or ""):
                    yield _sse(
                        {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "model": payload.model,
                            "provider_backend": None,
                            "provider_model_id": None,
                            "provider_attempts": [],
                            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                        }
                    )
                yield _sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "model": payload.model,
                        "provider_backend": None,
                        "provider_model_id": None,
                        "provider_attempts": [],
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                yield _sse("[DONE]")

            return StreamingResponse(gen(), media_type="text/event-stream")

        response = ChatCompletionsResponse(
            id=completion_id,
            model=payload.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    finish_reason=finish_reason,
                    message=ChatCompletionOutputMessage(content=content, tool_calls=tool_calls),
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
        return JSONResponse(response.model_dump(mode="json"))

    @app.post("/v1/responses")
    async def responses(payload: ResponsesRequest):
        inputs = [payload.input] if isinstance(payload.input, str) else payload.input
        response_id = f"resp-{uuid.uuid4().hex[:12]}"
        items = [
            ResponseOutputItem(
                id=f"{response_id}_item_{index}",
                index=index,
                content=[
                    ResponseOutputContent(
                        type="text",
                        text=f"stub response to: {text}",
                        token_count=len(text.split()),
                    )
                ],
            )
            for index, text in enumerate(inputs)
        ]
        total_tokens = sum(item.content[0].token_count for item in items)
        usage = EmbeddingUsageResponse(prompt_tokens=total_tokens, total_tokens=total_tokens)

        if payload.stream:

            async def gen():
                yield _sse(
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "status": "in_progress",
                            "model": payload.model,
                            "provider_backend": None,
                            "provider_model_id": None,
                            "provider_attempts": [],
                        },
                    }
                )
                for item in items:
                    yield _sse(
                        {
                            "type": "response.output_item.done",
                            "output_index": item.index,
                            "item": {**item.model_dump(mode="json"), "response_id": response_id},
                        }
                    )
                yield _sse(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "status": "completed",
                            "model": payload.model,
                            "provider_backend": None,
                            "provider_model_id": None,
                            "provider_attempts": [],
                            "usage": usage.model_dump(mode="json"),
                        },
                    }
                )
                yield _sse("[DONE]")

            return StreamingResponse(gen(), media_type="text/event-stream")

        response = ResponsesResponse(id=response_id, model=payload.model, output=items, usage=usage)
        return JSONResponse(response.model_dump(mode="json"))

    return app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_until_serving(port: int, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_event_loop().time() > deadline:
                raise
            await asyncio.sleep(0.02)
            continue
        writer.close()
        await writer.wait_closed()
        return


class StubServerFixture:
    """Context manager that runs `build_stub_app()` over a real HTTP socket in a background
    thread (a real `uvicorn.Server`, not an ASGI in-memory transport shortcut) -- so the standard
    `openai` client under test talks to it over genuine HTTP/SSE, the same as it would to a real
    engine.

    Also usable directly, outside this package's own test suite, as a genuine "try it locally, no
    live Algenta engine required" fixture -- see this package's README's "Try it locally" section.
    """

    def __init__(self) -> None:
        self.port: int = 0
        self.base_url: str = ""
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> StubServerFixture:
        self.port = _free_port()
        config = uvicorn.Config(build_stub_app(), host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 5.0
        while not self._server.started:
            if time.time() > deadline:
                raise TimeoutError("stub server did not start in time")
            time.sleep(0.02)
        self.base_url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.should_exit = True
        assert self._thread is not None
        self._thread.join(timeout=5.0)
