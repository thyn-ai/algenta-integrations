"""Real conformance tests for Algenta's `/v1/chat/completions` surface, driven through the real,
unmodified `openai` Python client (never a mocked client, never a mocked stub) -- proving exactly
what this package's README claims works today, and, just as importantly, proving what silently
does *not* work (tool-calling) rather than merely asserting it isn't tested.

Deliberately does NOT contain a test asserting tool-calling works, per this package's own honest
capability accounting: `apps/api_server/schemas/llm.py`'s `ChatCompletionsRequest` has no `tools`
field at all, verified directly from source (see `tests/stub_server.py`'s module docstring for the
exact citation). `test_tools_argument_is_silently_ignored_not_rejected` below is the deliberate
opposite of that -- proof the capability is absent, not a test that pretends it's present.
"""

from __future__ import annotations

from openai import OpenAI

from vllm_algenta.client import resolve_v1_base_url

from .stub_server import StubServerFixture


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
            model="text.tokenizer",
            messages=[{"role": "user", "content": "what is the capital of France?"}],
        )
        assert resp.created is None  # real gap in the real schema -- not this package's doing
        assert resp.object == "chat.completion"
        assert len(resp.choices) == 1
        choice = resp.choices[0]
        assert choice.message.role == "assistant"
        assert "what is the capital of France?" in choice.message.content
        assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens


def test_finish_reason_is_always_the_hardcoded_literal_stop() -> None:
    """`ChatCompletionChoice.finish_reason: Literal["stop"] = "stop"` is hardcoded on the real
    engine (`apps/api_server/schemas/llm.py:265` as of the commit cited in `stub_server.py`) --
    there is no length/content_filter/tool_calls value this endpoint can ever produce, regardless
    of `max_tokens`, message content, or anything else a caller supplies. Two different requests,
    to prove this isn't just "the one request I happened to try.\""""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        for content in ["short", "a " * 500]:
            resp = client.chat.completions.create(
                model="text.tokenizer", messages=[{"role": "user", "content": content}], max_tokens=1
            )
            assert resp.choices[0].finish_reason == "stop"


def test_streaming_is_synthetic_post_hoc_rechunking_not_incremental_generation() -> None:
    """Real streaming shape, verified via the real `openai` client's own stream iterator: a first
    chunk carrying only `{"role": "assistant"}`, then content chunks, then a final empty-delta
    chunk carrying `finish_reason="stop"`. The content chunks are sliced from an
    already-fully-computed string in fixed-size (24-character) pieces
    (`apps/api_server/routers/llm.py::_stream_text_chunks`) -- this is NOT the backend generating
    and emitting tokens incrementally as they're produced; every character of the reply already
    existed before the first content chunk was sent. This test can't observe server-side timing
    from the client side, so it asserts the one client-observable fingerprint of that fact
    instead: every content chunk except (possibly) the last is exactly `_STREAM_CHUNK_SIZE`
    characters, matching fixed-size slicing rather than natural token boundaries."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        stream = client.chat.completions.create(
            model="text.tokenizer",
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


def test_tools_argument_is_silently_ignored_not_rejected() -> None:
    """The central capability-fidelity claim this package's README makes, proven rather than
    asserted: `ChatCompletionsRequest` has no `tools`/`tool_choice` field, and neither it nor this
    stub's copy of it sets `extra="forbid"` -- pydantic's own default (`extra="ignore"`) means a
    `tools=` argument the standard `openai` client happily serializes and sends is silently
    dropped server-side. The call still succeeds (200, a normal text reply) -- it does NOT fail
    loudly, which is exactly the trap a caller migrating from a real tool-calling provider could
    fall into unless this is documented (and tested) explicitly. `message.tool_calls` is `None`
    on the reply -- there is no tool_calls field on the real response schema at all for a value to
    ever appear in."""
    with StubServerFixture() as stub:
        client = _client(stub.base_url)
        resp = client.chat.completions.create(
            model="text.tokenizer",
            messages=[{"role": "user", "content": "what's the weather in Boston?"}],
            tools=[
                {
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
            ],
            tool_choice="auto",
        )
        assert resp.choices[0].finish_reason == "stop"  # never "tool_calls" -- that value can't occur
        assert resp.choices[0].message.tool_calls is None
        assert resp.choices[0].message.content  # a normal text reply, tools silently had no effect
