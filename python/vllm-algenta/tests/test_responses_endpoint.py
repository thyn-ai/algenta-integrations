"""Real conformance tests for Algenta's `/v1/responses` surface.

`ResponsesRequest` (`apps/api_server/schemas/llm.py` in `thyn-ai/algenta`) has no `tools`,
`previous_response_id`, or approval-related field, and its `input` is `str | list[str]` -- plain
strings, not the structured `[{role, content}]` message-array shape OpenAI's real Responses API
accepts. The streaming SSE event sequence (`apps/api_server/routers/llm.py::_responses_stream`)
emits exactly three event `type`s -- `response.created`, `response.output_item.done`,
`response.completed` -- and nothing else: no `response.output_text.delta` (no incremental text
events at all), no tool-call event, no approval-required event. This file asserts that event
sequence directly over raw SSE, independent of how leniently any particular version of the
`openai` Python client happens to parse it (verified separately, informally, that the SDK's own
`.responses.create()` does not raise against this shape either -- but this test suite does not
depend on that leniency holding across SDK versions to make its actual claim).
"""

from __future__ import annotations

import json

import httpx

from .stub_server import StubServerFixture


def _parse_sse_events(raw: str) -> list[object]:
    events: list[object] = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[len("data: ") :]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


def test_responses_basic_non_streaming() -> None:
    with StubServerFixture() as stub:
        resp = httpx.post(f"{stub.base_url}/v1/responses", json={"model": "text.tokenizer", "input": "hello"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert len(body["output"]) == 1
        assert body["output"][0]["content"][0]["text"] == "stub response to: hello"


def test_responses_accepts_a_list_of_strings_not_structured_messages() -> None:
    """`input` is `str | list[str]` on the real schema -- a caller migrating from OpenAI's real
    Responses API and sending `input=[{"role": "user", "content": "hi"}]` (a structured message
    array) is sending something this endpoint's schema does not model as such at all; a plain
    `list[str]` is the only array shape the real request schema accepts."""
    with StubServerFixture() as stub:
        resp = httpx.post(
            f"{stub.base_url}/v1/responses",
            json={"model": "text.tokenizer", "input": ["first", "second"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["output"]) == 2
        assert body["output"][0]["content"][0]["text"] == "stub response to: first"
        assert body["output"][1]["content"][0]["text"] == "stub response to: second"


def test_responses_streaming_emits_exactly_three_event_types_and_nothing_else() -> None:
    with httpx.Client(timeout=10.0) as client, StubServerFixture() as stub:
        with client.stream(
            "POST",
            f"{stub.base_url}/v1/responses",
            json={"model": "text.tokenizer", "input": ["a", "b"], "stream": True},
            headers={"accept": "text/event-stream"},
            timeout=10.0,
        ) as resp:
            raw = "".join(resp.iter_text())

    events = _parse_sse_events(raw)
    assert events[-1] == "[DONE]"
    event_types = [e["type"] for e in events[:-1]]

    # Exactly: one response.created, one response.output_item.done per input item, one
    # response.completed -- in that order. No response.output_text.delta, no tool/approval event
    # of any kind -- those event names never appear anywhere in this list, because the real
    # engine never emits them on this route (see this file's module docstring).
    assert event_types == [
        "response.created",
        "response.output_item.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert events[0]["response"]["status"] == "in_progress"
    assert events[-2]["response"]["status"] == "completed"
    assert "usage" in events[-2]["response"]

    observed_types = {e["type"] for e in events[:-1]}
    forbidden_types = {
        "response.output_text.delta",
        "response.tool_call.created",
        "response.approval_required",
        "response.in_progress",
    }
    assert not (observed_types & forbidden_types)
