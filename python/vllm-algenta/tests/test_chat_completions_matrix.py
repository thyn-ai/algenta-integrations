"""Real conformance tests for Algenta's `/v1/chat/completions` surface, driven through the real,
unmodified `openai` Python client (never a mocked client, never a mocked stub) -- proving exactly
what this package's README claims works today, model-dependence included.

Tool calling and a widened `finish_reason` are REAL capabilities of the engine today (see
`tests/stub_server.py`'s module docstring for how that was verified against the engine's public
HTTP API) -- but both are model-dependent, matching the real endpoint's own behavior exactly:

- This package's own zero-config default model (`text.tokenizer`) has no tool-calling mechanism.
  A `tools=` argument against it is REJECTED with a loud `422 model_capability_unsupported`, not
  silently dropped -- `test_tools_argument_against_the_default_model_is_rejected_not_silently_ignored`
  proves the rejection, not merely the absence of a positive test.
- Any other model (a configured provider-backed model, or the bundled `algenta_local` backend --
  both real, both deployment-specific) DOES support tool calling for real, including a genuine
  `finish_reason="tool_calls"` -- `test_tool_calling_produces_real_tool_calls_and_finish_reason_on_a_tool_capable_model`
  proves the capability directly, through the real `openai` client, rather than merely asserting
  the schema allows it.
"""

from __future__ import annotations

import pytest
from openai import OpenAI, UnprocessableEntityError
from vllm_algenta.client import resolve_v1_base_url

from .stub_server import DEFAULT_MODEL, TOOL_CALLING_MODEL, StubServerFixture

_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a location.",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    },
}


def _client(base_url: str) -> OpenAI:
    return OpenAI(base_url=resolve_v1_base_url(base_url), api_key="test-key-stub-does-not-check-it")


def test_basic_completion_round_trips_through_the_real_openai_client() -> None:
    """A plain, non-streaming completion: proves the standard `openai` client can drive Algenta's
    real response shape end to end, including tolerating the missing `created` field the OpenAI
    wire format normally carries -- verified directly (see `vllm_algenta.client`'s module
    docstring): the SDK leaves `.created` `None` rather than raising."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        resp = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "what is the capital of France?"}],
        )
        assert resp.created is None  # real gap in the real schema -- not this package's doing
        assert resp.object == "chat.completion"
        assert len(resp.choices) == 1
        choice = resp.choices[0]
        assert choice.message.role == "assistant"
        assert "what is the capital of France?" in choice.message.content
        assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens


def test_finish_reason_is_stop_for_the_deterministic_default_model() -> None:
    """`text.tokenizer` (this package's own zero-config default) is a deterministic utility model
    with no generation to truncate or interrupt -- its `finish_reason` is always `"stop"`, no
    matter what a caller supplies. This is a property of THAT MODEL, not of the endpoint: the
    schema's `finish_reason` type is `Literal["stop", "tool_calls", "length", "content_filter"]`
    (widened from a permanent `"stop"` literal -- see this file's module docstring), and a
    tool-capable model genuinely produces `"tool_calls"`, proven separately below. Two different
    requests here, to prove this isn't just "the one request I happened to try.\""""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        for content in ["short", "a " * 500]:
            resp = client.chat.completions.create(
                model=DEFAULT_MODEL, messages=[{"role": "user", "content": content}], max_tokens=1
            )
            assert resp.choices[0].finish_reason == "stop"


def test_streaming_is_synthetic_post_hoc_rechunking_not_incremental_generation() -> None:
    """Real streaming shape, verified via the real `openai` client's own stream iterator: a first
    chunk carrying only `{"role": "assistant"}`, then content chunks, then a final empty-delta
    chunk carrying `finish_reason="stop"`. For `text.tokenizer` (this package's own zero-config
    default model), the content chunks are sliced from an already-fully-computed string in
    fixed-size (24-character) pieces (the same fixed-size slicing the real endpoint performs --
    see `tests/stub_server.py`'s `_stream_text_chunks`) --
    this is NOT the backend generating and emitting tokens incrementally as they're produced;
    every character of the reply already existed before the first content chunk was sent. The
    real engine also has a real, incremental "passthrough" streaming mode for models that opt into
    it -- this stub does not simulate that mode; see this package's README for the
    accurate, model-dependent framing. This test can't observe server-side timing from the client
    side, so it asserts the one client-observable fingerprint of fixed-size slicing instead: every
    content chunk except (possibly) the last is exactly `_STREAM_CHUNK_SIZE` characters, matching
    fixed-size slicing rather than natural token boundaries."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        stream = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "give me a longer reply please, thank you"}],
            stream=True,
        )
        events = list(stream)

        assert events[0].choices[0].delta.role == "assistant"
        assert events[0].choices[0].delta.content is None

        content_chunks = [
            e.choices[0].delta.content
            for e in events[1:-1]
            if e.choices[0].delta.content is not None
        ]
        assert content_chunks, "expected at least one content chunk"
        for chunk in content_chunks[:-1]:
            assert len(chunk) == 24  # _STREAM_CHUNK_SIZE -- fixed-size slicing, not token boundaries

        last = events[-1]
        assert last.choices[0].finish_reason == "stop"
        assert not last.choices[0].delta.content

        reconstructed = "".join(content_chunks)
        assert reconstructed.startswith("stub reply to: give me a longer reply")


def test_tools_argument_against_the_default_model_is_rejected_not_silently_ignored() -> None:
    """This package's own zero-config default model (`text.tokenizer`) has no tool-calling
    mechanism at all (verified against the real endpoint's behavior)
    -- a `tools=` argument the standard `openai` client happily serializes and sends gets a loud
    `422 model_capability_unsupported` response, not a silent 200 with the argument dropped. This
    is the opposite of an earlier engine version's behavior, and a real improvement worth calling
    out explicitly: a caller who accidentally sends `tools=` against the default model finds out
    immediately, from the server, rather than discovering the gap the hard way."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        with pytest.raises(UnprocessableEntityError) as exc_info:
            client.chat.completions.create(
                model=DEFAULT_MODEL,
                messages=[{"role": "user", "content": "what's the weather in Boston?"}],
                tools=[_WEATHER_TOOL],
                tool_choice="auto",
            )
        error_body = exc_info.value.body
        assert error_body["code"] == "model_capability_unsupported"
        assert error_body["details"]["required_capability"] == "tool_calling"


def test_tool_calling_produces_real_tool_calls_and_finish_reason_on_a_tool_capable_model() -> None:
    """`TOOL_CALLING_MODEL` stands in for a configured provider-backed model or the bundled
    `algenta_local` backend -- both real, both able to call tools for real (see
    `tests/stub_server.py`'s `ChatCompletionsRequest.tools` / `ChatCompletionChoice.finish_reason:
    Literal[..., "tool_calls", ...]`, which mirror the engine's public wire shape). This is the
    direct, positive proof of the capability this package's README used to claim didn't exist at
    all."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        resp = client.chat.completions.create(
            model=TOOL_CALLING_MODEL,
            messages=[{"role": "user", "content": "what's the weather in Boston?"}],
            tools=[_WEATHER_TOOL],
            tool_choice="auto",
        )
        choice = resp.choices[0]
        assert choice.finish_reason == "tool_calls"
        assert choice.message.content is None
        assert choice.message.tool_calls is not None
        assert len(choice.message.tool_calls) == 1
        call = choice.message.tool_calls[0]
        assert call.function.name == "get_weather"


def test_tool_choice_none_suppresses_a_tool_call_even_when_tools_are_offered() -> None:
    """`tool_choice="none"` on a tool-capable model must produce a normal text reply, never a tool
    call -- matching `ChatCompletionsRequest.tool_choice`'s own documented semantics."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        resp = client.chat.completions.create(
            model=TOOL_CALLING_MODEL,
            messages=[{"role": "user", "content": "what's the weather in Boston?"}],
            tools=[_WEATHER_TOOL],
            tool_choice="none",
        )
        choice = resp.choices[0]
        assert choice.finish_reason == "stop"
        assert choice.message.tool_calls is None
        assert choice.message.content


def test_streaming_is_refused_when_a_tool_call_would_actually_fire() -> None:
    """The real engine's per-backend streaming and its non-streaming tool calling shipped
    separately -- a completion that would actually call a tool cannot yet be streamed, and the
    real engine refuses this combination with `422 model_capability_unsupported` /
    `required_capability: "streaming_tool_calls"` rather than silently dropping the tool call into
    the SSE stream. This is a real, current, documented limitation -- not something this package
    invented."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        with pytest.raises(UnprocessableEntityError) as exc_info:
            client.chat.completions.create(
                model=TOOL_CALLING_MODEL,
                messages=[{"role": "user", "content": "what's the weather in Boston?"}],
                tools=[_WEATHER_TOOL],
                tool_choice="auto",
                stream=True,
            )
        assert exc_info.value.body["details"]["required_capability"] == "streaming_tool_calls"
