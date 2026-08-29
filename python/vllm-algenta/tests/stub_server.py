"""A minimal, deterministic, real stub of Algenta's actual `/v1/chat/completions` and
`/v1/responses` behavior -- a real FastAPI app on a real HTTP socket, never a mock.

Every request/response field below is copied BY HAND from
`apps/api_server/schemas/llm.py` and `apps/api_server/routers/llm.py` in `thyn-ai/algenta`
(verified directly against commit `1b95f7d71dd3aaedf546cc76cb04574fd35e4dec` of that repository's
`main` branch during this package's design -- re-verify against current `main` before trusting
this file blindly if it's been a while) -- never imported from there, which
`scripts/check-no-engine-dependency.py` forbids this repository from doing regardless. Copying the
publicly-observable request/response SHAPE by hand, with no engine source or logic inside it, is
the same pattern `contracts/integration-tool-contract.json`'s own `provenance_note` documents for
this repository's MCP tool contract.

What this stub deliberately reproduces, because these are the load-bearing, easy-to-get-wrong
facts this package's README stakes claims on:

- `ChatCompletionsRequest` has no `tools` / `tool_choice` field, and neither pydantic model here
  sets `model_config = ConfigDict(extra="forbid")` (confirmed: neither does the real one) -- so a
  caller who passes `tools=[...]` through the standard `openai` client gets a 200 response with
  the `tools` argument silently dropped, never a validation error. See
  `test_tools_argument_is_silently_ignored_not_rejected` in
  `tests/test_chat_completions_matrix.py`.
- `ChatCompletionChoice.finish_reason` is hardcoded to the literal `"stop"` -- there is no other
  value this stub (or the real engine) can produce.
- Streaming is post-hoc rechunking of an already-fully-computed string (`_stream_text_chunks`,
  `chunk_size=24`), not real incremental per-token generation -- copied verbatim from
  `apps/api_server/routers/llm.py`'s own function of the same name.
- `/v1/responses` streaming emits exactly three event types --
  `response.created` / `response.output_item.done` / `response.completed` -- and nothing else
  (no `response.output_text.delta`, no tool/approval events of any kind).

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from typing import Any, Literal, Optional, Union

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------------------------
# Request/response models -- hand-copied field-for-field from apps/api_server/schemas/llm.py.
# Deliberately excludes every field belonging to unrelated routes (tokenize, embeddings, rerank,
# ...) that this package's own README makes no claim about.
# ---------------------------------------------------------------------------------------------


class ChatCompletionInputMessage(BaseModel):
    role: Literal["system", "user", "assistant", "developer"]
    content: str


class ChatCompletionsRequest(BaseModel):
    model: str = "text.tokenizer"
    messages: list[ChatCompletionInputMessage] = Field(..., min_length=1)
    stream: bool = False
    max_tokens: Optional[int] = Field(default=None, ge=1)
    temperature: Optional[float] = Field(default=None, ge=0.0)
    top_p: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    seed: Optional[int] = None
    # No `tools` / `tool_choice` field exists here -- matching the real request schema exactly.
    # Pydantic's own default (`extra="ignore"`, since neither this class nor the real one sets
    # `model_config`) means an extra field a caller sends anyway (tools, functions, ...) is
    # silently dropped during validation, not rejected.


class ChatCompletionOutputMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class ChatCompletionChoice(BaseModel):
    index: int
    finish_reason: Literal["stop"] = "stop"  # hardcoded on the real engine too -- never anything else
    message: ChatCompletionOutputMessage


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ProviderAttemptResponse(BaseModel):
    provider_backend: str
    provider_model_id: str
    outcome: Literal["selected", "failed"]
    error_code: Optional[str] = None


class ChatCompletionsResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    provider_backend: Optional[str] = None
    provider_model_id: Optional[str] = None
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    # No `created` field -- matching the real response exactly (verified separately: the
    # standard `openai` Python client parses this response shape without error regardless,
    # leaving its own `.created` attribute `None` -- see this package's README).


class ResponsesRequest(BaseModel):
    model: str = "text.tokenizer"
    input: Union[str, list[str]]
    dimensions: int = Field(default=64, gt=0, le=4096)
    stream: bool = False


class ResponseOutputContent(BaseModel):
    type: Literal["tokenization", "embedding", "text"]
    text: str
    tokens: Optional[list[str]] = None
    token_count: int
    embedding: Optional[list[float]] = None


class ResponseOutputItem(BaseModel):
    id: str
    object: Literal["response.output"] = "response.output"
    index: int
    provider_backend: Optional[str] = None
    provider_model_id: Optional[str] = None
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
    provider_backend: Optional[str] = None
    provider_model_id: Optional[str] = None
    provider_attempts: list[ProviderAttemptResponse] = Field(default_factory=list)
    output: list[ResponseOutputItem]
    usage: EmbeddingUsageResponse


# ---------------------------------------------------------------------------------------------
# Deterministic "completion" logic -- NOT a reimplementation of the real engine's tokenizer or
# any model. This stub's whole job is to reproduce the real *shape* (schema, hardcoded
# finish_reason, rechunked-streaming mechanics) faithfully; the actual text it returns is a
# simple, deterministic function of the input, clearly synthetic, never claimed as anything else.
# ---------------------------------------------------------------------------------------------

_STREAM_CHUNK_SIZE = 24  # matches apps/api_server/routers/llm.py's _STREAM_CHUNK_SIZE exactly


def _stream_text_chunks(text: str, *, chunk_size: int = _STREAM_CHUNK_SIZE) -> list[str]:
    """Copied verbatim from `apps/api_server/routers/llm.py::_stream_text_chunks` -- post-hoc
    slicing of an already-fully-computed string, not incremental generation."""
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _deterministic_reply(messages: list[ChatCompletionInputMessage]) -> str:
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    return f"stub reply to: {last_user}"


def _sse(payload: Any) -> str:
    if payload == "[DONE]":
        return "data: [DONE]\n\n"
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def build_stub_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: ChatCompletionsRequest):
        content = _deterministic_reply(payload.messages)
        completion_id = f"cmpl-{uuid.uuid4().hex[:12]}"
        prompt_tokens = sum(len(m.content.split()) for m in payload.messages)
        completion_tokens = len(content.split())

        if payload.stream:

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
                for chunk in _stream_text_chunks(content):
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
                ChatCompletionChoice(index=0, message=ChatCompletionOutputMessage(content=content))
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
    engine."""

    def __init__(self) -> None:
        self.port: int = 0
        self.base_url: str = ""
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StubServerFixture":
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
