# vllm-algenta

Point vLLM -- or any OpenAI-client-based consumer -- at Algenta's own OpenAI-compatible
`/v1/chat/completions` and `/v1/responses` surface: `vllm_algenta.client.build_client` /
`build_async_client`, two thin helpers that resolve the right `base_url`/`api_key` for the
standard, unmodified `openai` Python client.

**Direction, stated plainly, because it's easy to assume the opposite:** this package is vLLM (or
any other OpenAI-client-shaped consumer) calling **into** Algenta -- never Algenta calling **out**
to a vLLM-hosted model. That reverse direction isn't buildable in this repository at all: it would
mean depending on or reimplementing engine-side model-serving/routing logic, which
`scripts/check-no-engine-dependency.py` forbids, and it doesn't match this repository's own
established shape ("a framework talks to Algenta") that every other package here follows.

## Install

```bash
pip install vllm-algenta
```

This package depends on exactly one thing: the real, published
[`openai`](https://pypi.org/project/openai/) Python client (`>=1.50`). No `algenta-sdk`
dependency -- see [Why no `algenta-sdk` dependency](#why-no-algenta-sdk-dependency).

## Self-hosted-first

```python
from vllm_algenta.client import build_client

client = build_client(base_url="http://localhost:8000", api_key="...")  # your own self-hosted engine
resp = client.chat.completions.create(
    model="text.tokenizer",
    messages=[{"role": "user", "content": "Summarize this quarter's decision log."}],
)
print(resp.choices[0].message.content)
```

`base_url` resolves, in order, from the `base_url=` argument, the `ALGENTA_BASE_URL` environment
variable, then `http://localhost:8000` -- never an Algenta-hosted default. `api_key` resolves from
`api_key=` or `ALGENTA_API_KEY`; unlike `base_url`, there is **no** fallback default for this one
-- `build_client`/`build_async_client` raise a clear `RuntimeError` instead of silently sending a
placeholder credential that would only fail later with a confusing 401. See [Auth is required, not
optional](#auth-is-required-not-optional) below for why.

Note the exact value `ALGENTA_BASE_URL` means here: **the engine's bare HTTP origin**
(`http://localhost:8000`, no path suffix) -- this package appends `/v1` itself. Some MCP-based
sibling packages in this repository (`pydantic-ai-algenta`, for one) bake a `/mcp` suffix directly
into their own `ALGENTA_BASE_URL` fallback constant, because that whole string gets handed
straight to an MCP client. Don't copy one package's `ALGENTA_BASE_URL` value verbatim into
another's environment -- see `vllm_algenta/client.py`'s module docstring for the full explanation.

## Capability status -- what works today, what does not, read this before you build on it

Verified directly against `apps/api_server/schemas/llm.py` and `apps/api_server/routers/llm.py` in
`thyn-ai/algenta` (the engine's own source repository), not assumed from documentation or an
internal planning note -- an earlier internal memory claimed materially more capability than the
real schema/router code has. Corrected here, plainly:

| Capability | Status | Evidence |
|---|---|---|
| Basic chat completion (`/v1/chat/completions`, non-streaming) | **Works** | `tests/test_chat_completions_matrix.py::test_basic_completion_round_trips_through_the_real_openai_client` |
| `finish_reason` reflecting how generation actually ended | **Does not exist** -- hardcoded `"stop"` | `ChatCompletionChoice.finish_reason: Literal["stop"] = "stop"`, `apps/api_server/schemas/llm.py`. Never `"length"`, `"content_filter"`, or `"tool_calls"` -- the last of those can't occur because tool-calling doesn't exist on this route at all (below). |
| Tool calling / function calling (`tools=`, `tool_choice=`) | **Does not exist** | `ChatCompletionsRequest` has no `tools` or `tool_choice` field, and neither it nor this package's stub sets `extra="forbid"` -- pydantic's default (`extra="ignore"`) means a `tools=` argument sent through the standard `openai` client is **silently dropped, not rejected**. The call still returns 200 with a plain text reply. See `test_tools_argument_is_silently_ignored_not_rejected` -- reproduced directly, not asserted from the schema alone. **Do not build tool-calling logic against this endpoint; it will appear to work (no error) and never actually call anything.** |
| Streaming (`stream=true`) | **Works, but is synthetic** | `apps/api_server/routers/llm.py::_chat_completion_stream` computes the full reply first, then slices it into fixed-size (24-character) chunks (`_stream_text_chunks`) and re-emits them as SSE `chat.completion.chunk` events. This is post-hoc rechunking of an already-complete string, **not the backend generating and emitting tokens incrementally as they're produced**. A caller cannot use `stream=true` here to reduce time-to-first-useful-content the way real incremental backend streaming would -- the full completion already existed before the first chunk was sent. |
| `/v1/responses` unified envelope | **Works for its own, narrower shape** | `ResponsesRequest.input` is `str \| list[str]` -- plain strings, not the structured `[{role, content}]` message-array shape OpenAI's real Responses API accepts. Streaming emits exactly three event types -- `response.created`, `response.output_item.done`, `response.completed` -- and nothing else: no `response.output_text.delta` (no incremental text events at all), no tool-call event, no approval-required event. See `tests/test_responses_endpoint.py::test_responses_streaming_emits_exactly_three_event_types_and_nothing_else`. |
| `previous_response_id` / multi-turn Responses continuation | **Does not exist** | No such field anywhere on `ResponsesRequest`. |

None of this is a defect this package is responsible for or can work around from the client side
-- it's an honest description of the connected engine's real, current LLM API surface, so a reader
building against it doesn't have to rediscover the gap the hard way. Track C in this repository's
broader roadmap is where native tool-calling and real incremental streaming, if they land, would
need to be added engine-side; this package makes no claim about when or whether that happens.

## Auth is required, not optional

Every route on Algenta's LLM API router carries `Depends(require_verified_email)` and
`Depends(bind_tenant_keys)` (`apps/api_server/routers/llm.py`'s own `APIRouter(...)`
declaration) -- every request needs an authenticated, org-bound caller identity, in every
deployment mode this package has been able to verify from source. `build_client`/
`build_async_client` treat a credential as required in practice for that reason: they raise
`RuntimeError` if neither `api_key=` nor `ALGENTA_API_KEY` resolves, rather than sending a
placeholder string that would just 401 deep inside the `openai` SDK's own error handling. This
package's own test suite talks to a stub that does not enforce auth at all (out of scope for a
stub whose job is testing the `/v1/chat/completions` / `/v1/responses` request/response *shape*,
not the accounts/multi-tenancy stack) -- so tests pass an arbitrary non-secret string as `api_key`.

## Why no `algenta-sdk` dependency

Same reasoning as `litellm-algenta` (D4), `llamaindex-algenta`, and `ray-serve-algenta` (D6's
other two lanes): every package in this repository may depend on at most one Algenta-owned thing,
the published `algenta-sdk` client -- but only if something in the package would actually use it.
`vllm_algenta.client` computes a URL and forwards a credential to the standard `openai` client; it
never constructs an MCP client, never calls a tool, and has no use for an SDK object of any kind.
Declaring `algenta-sdk` anyway, unused, would repeat exactly the leftover-placeholder-dependency
pattern an adversarial review is on record catching elsewhere in this repository's history.

## Testing this package

The conformance suite runs against a real stub HTTP server (`tests/stub_server.py`, a real FastAPI
app on a real `uvicorn` socket, never a mock) whose request/response Pydantic models were copied
by hand, field-for-field, from `apps/api_server/schemas/llm.py` in `thyn-ai/algenta` -- see that
file's own module docstring for the exact commit this was verified against, and re-verify against
current `main` before trusting it blindly if it's been a while. Every test drives the real,
unmodified `openai` Python client (or, for the `/v1/responses` SSE event-sequence assertions,
real `httpx` directly against the raw event stream) -- never a mocked client.

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest vllm-algenta -v
```

`tests/test_chat_completions_matrix.py` covers the `/v1/chat/completions` matrix named in this
package's own scope: a basic completion, the hardcoded `finish_reason`, synthetic rechunked
streaming, and -- deliberately -- proof that a `tools=` argument is silently ignored rather than a
test that pretends tool-calling works. `tests/test_responses_endpoint.py` covers the
`/v1/responses` envelope, including the exact three-event-type streaming sequence.
`tests/test_client_config.py` is fast, no-network unit coverage of `vllm_algenta.client`'s own
`base_url`/`api_key` resolution logic.
