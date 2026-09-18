"""Real conformance tests for this stub's own, currently-narrower `/v1/responses` surface.

This file's assertions describe what THIS STUB implements: `input` as `str | list[str]` (plain
strings, each an independent single-turn request), and a streaming SSE event sequence that emits
exactly three event `type`s -- `response.created`, `response.output_item.done`,
`response.completed` -- and nothing else.

IMPORTANT, verified against the engine's public HTTP API:
the REAL engine's `/v1/responses` surface has since grown its own `tools` / `tool_choice` /
`parallel_tool_calls` / `previous_response_id` support and a typed OpenResponses-style input-array
shape for `input` (see this file's own `ResponsesRequest` note in `tests/stub_server.py`) -- a
separate, later change from the Chat Completions tool-calling fix this package's own README and
`tests/test_chat_completions_matrix.py` were just updated for. This stub and this test file have
NOT been updated to match that yet (tracked as a follow-up); do not read the assertions below as a
claim about what the real `/v1/responses` endpoint can do today -- see this package's README's
capability table for the accurate, currently-caveated framing of this specific gap.
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
    """`input` as `str | list[str]` is the shape THIS STUB implements. The real schema also now
    accepts a third, typed OpenResponses-style input-array shape (`list[dict]` items carrying
    their own `"type"`) that this stub does not yet reproduce -- see this file's module docstring
    for the honest caveat; do not read this test as a claim that a plain `list[str]` is the only
    array shape the real engine accepts today."""
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
