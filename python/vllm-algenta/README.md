# vllm-algenta

[![PyPI](https://img.shields.io/pypi/v/vllm-algenta.svg)](https://pypi.org/project/vllm-algenta/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../README.md)

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

## Prerequisites

- **A running, self-hosted Algenta engine.** This package never talks to any Algenta-operated
  cloud service -- it always calls the engine's HTTP API at your own `ALGENTA_BASE_URL`
  (`http://localhost:8000` by default). If you don't have an engine running yet, see
  [Algenta's Quickstart guide](https://docs.algenta.ai/quickstart) -- it installs the engine and
  mints your first API key in about two minutes.
- **An Algenta API key**, from that same install step, exported as `ALGENTA_API_KEY`.
- **The `openai` Python package** (`>=1.50`) -- installed automatically as this package's one
  dependency.

Don't have an engine handy right now? Skip to [Try it locally](#try-it-locally-no-live-engine-required)
-- it runs this package's own real test stub instead, no engine or API key required.

## Install

```bash
pip install vllm-algenta
```

This package depends on exactly one thing: the real, published
[`openai`](https://pypi.org/project/openai/) Python client (`>=1.50`). No `algenta-sdk`
dependency -- see [Why no `algenta-sdk` dependency](#why-no-algenta-sdk-dependency).

## Quickstart

```python
import os

from vllm_algenta.client import build_client

client = build_client(
    base_url="http://localhost:8000",        # your own self-hosted engine
    api_key=os.environ["ALGENTA_API_KEY"],    # required -- raises immediately if unset, not "..."
)
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
placeholder credential that would only fail later with a confusing 401. The snippet above makes
that requirement visible at the call site too: `os.environ["ALGENTA_API_KEY"]` raises a `KeyError`
immediately, in your own code, the moment the variable is unset -- never a valid-looking string
like `"..."` that quietly succeeds here and only fails much later, deep inside an HTTP call, with a
confusing 401. See [Auth is required, not optional](#auth-is-required-not-optional) below for why.

Note the exact value `ALGENTA_BASE_URL` means here: **the engine's bare HTTP origin**
(`http://localhost:8000`, no path suffix) -- this package appends `/v1` itself. Some MCP-based
sibling packages in this repository (`pydantic-ai-algenta`, for one) bake a `/mcp` suffix directly
into their own `ALGENTA_BASE_URL` fallback constant, because that whole string gets handed
straight to an MCP client. Don't copy one package's `ALGENTA_BASE_URL` value verbatim into
another's environment -- see `vllm_algenta/client.py`'s module docstring for the full explanation.

## Try it locally (no live engine required)

This package's own conformance suite runs against a real stub Algenta HTTP server
(`tests/stub_server.py`) -- a real FastAPI app on a real `uvicorn` socket, never a mock. It's also
a genuine way to try this package's client without a running Algenta engine at all. From a
checkout of this repository:

```bash
cd python
uv sync --all-packages --all-extras
cd vllm-algenta
uv run --project .. python -c "
from tests.stub_server import StubServerFixture
from vllm_algenta.client import build_client

with StubServerFixture() as stub:
    # stub.base_url is a real local HTTP server -- no live Algenta engine involved.
    client = build_client(base_url=stub.base_url, api_key='local-stub-does-not-check-this')
    resp = client.chat.completions.create(
        model='text.tokenizer',
        messages=[{'role': 'user', 'content': \"Summarize this quarter's decision log.\"}],
    )
    print(resp.choices[0].message.content)
    print('finish_reason:', resp.choices[0].finish_reason)
"
```

This prints a real (synthetic, clearly-labeled) reply and `finish_reason: stop` -- run directly,
with nothing mocked out, confirming the client and the stub's request/response shape actually
work together end to end. See [Testing this package](#testing-this-package) for how the same stub
backs the full conformance suite, including the tool-calling round trip.

## Capability status -- what works today, what does not, read this before you build on it

Verified directly against the engine's public OpenAI-compatible API contract (the
`/v1/chat/completions` and `/v1/responses` request/response shapes it actually serves), not
assumed from documentation or an internal planning note -- an earlier version of this table
claimed materially *less* capability than the served API now has (tool calling and a widened
`finish_reason` shipped after this table was last checked). Corrected here, plainly,
model-dependence included:

| Capability | Status | Evidence |
|---|---|---|
| Basic chat completion (`/v1/chat/completions`, non-streaming) | **Works** | `tests/test_chat_completions_matrix.py::test_basic_completion_round_trips_through_the_real_openai_client` |
| `finish_reason` reflecting how generation actually ended | **Works, model-dependent** | Widened from a permanent `"stop"` to `Literal["stop", "tool_calls", "length", "content_filter"]` (`ChatCompletionChoice.finish_reason` in the engine's public chat-completions schema). This package's own zero-config default model (`text.tokenizer`) is a deterministic utility model with nothing to truncate or interrupt, so it still only ever produces `"stop"` -- see `test_finish_reason_is_stop_for_the_deterministic_default_model`. A configured provider-backed model, or the bundled `algenta_local` backend, can genuinely produce `"tool_calls"` -- see the next row -- and provider-reported `"length"`/`"content_filter"` values pass through uninterpreted. |
| Tool calling / function calling (`tools=`, `tool_choice=`) | **Works, model-dependent** | `ChatCompletionsRequest.tools` / `.tool_choice` / `.parallel_tool_calls` are real fields on the public request schema, forwarded to configured provider-backed models and to the bundled `algenta_local` backend -- see `test_tool_calling_produces_real_tool_calls_and_finish_reason_on_a_tool_capable_model`, a genuine round trip through the real `openai` client, not an assertion from the schema alone. **This package's own zero-config default model (`text.tokenizer`) still has no tool-calling mechanism at all** -- sending `tools=` against it is now REJECTED with a loud `422 model_capability_unsupported`, a real improvement over silently dropping the argument (see `test_tools_argument_against_the_default_model_is_rejected_not_silently_ignored`). Pick a tool-capable model deliberately; don't assume the default one is it. |
| Streaming (`stream=true`) | **Works; synthetic by default, model-dependent otherwise** | For this package's own zero-config default model, the engine computes the full reply first, then slices it into fixed-size (24-character) chunks and re-emits them as SSE `chat.completion.chunk` events -- post-hoc rechunking, not the backend generating and emitting tokens incrementally as they're produced. Since then, the engine also has a real, incremental "passthrough" streaming mode for models that opt into one -- this package's own test suite exercises only the synthetic default-model path (see `test_streaming_is_synthetic_post_hoc_rechunking_not_incremental_generation`), since faithfully exercising passthrough mode needs a real streaming backend. Separately: a completion that actually calls a tool cannot yet be streamed at all -- `stream=true` combined with an actual tool call is refused with `422 model_capability_unsupported` rather than silently dropping the tool call (see `test_streaming_is_refused_when_a_tool_call_would_actually_fire`). |
| `/v1/responses` unified envelope | **Works for a narrower shape than the real engine now supports -- see the caveat below** | This package's own stub/tests currently model `ResponsesRequest.input` as `str \| list[str]` and a three-event streaming sequence (`response.created` / `response.output_item.done` / `response.completed`, nothing else). **The real engine has since added its own `tools`/`tool_choice`/`parallel_tool_calls`/`previous_response_id` support and a typed OpenResponses-style input-array shape for `input`** -- a separate, later change from the Chat Completions fix this table's other rows describe. This package's `/v1/responses` coverage has not been updated to match yet; treat the rows above (Chat Completions) as the current, re-verified ones, and this row as a known, tracked gap rather than an accurate description of `/v1/responses` today. |
| `previous_response_id` / multi-turn Responses continuation | **Now exists on the real engine; not yet covered by this package** | See the `/v1/responses` row above. |

None of this is a defect this package is responsible for or can work around from the client side
-- it's an honest, current description of the connected engine's real LLM API surface, so a reader
building against it doesn't have to rediscover the gap the hard way. This package's own stub and
test suite are re-verified against the engine's served API on each update; re-verify against your
own engine version before trusting this table blindly.

## Auth is required, not optional

Every route on the engine's LLM API requires an authenticated, org-bound caller identity, in
every deployment mode this package has been able to verify. `build_client`/
`build_async_client` treat a credential as required in practice for that reason: they raise
`RuntimeError` if neither `api_key=` nor `ALGENTA_API_KEY` resolves, rather than sending a
placeholder string that would just 401 deep inside the `openai` SDK's own error handling. This
package's own test suite talks to a stub that does not enforce auth at all (out of scope for a
stub whose job is testing the `/v1/chat/completions` / `/v1/responses` request/response *shape*,
not the accounts/multi-tenancy stack) -- so tests pass an arbitrary non-secret string as `api_key`.

## Why no `algenta-sdk` dependency

Same reasoning as `litellm-algenta`, `llamaindex-algenta`, and `ray-serve-algenta`: every
package in this repository may depend on at most one Algenta-owned thing,
the published `algenta-sdk` client -- but only if something in the package would actually use it.
`vllm_algenta.client` computes a URL and forwards a credential to the standard `openai` client; it
never constructs an MCP client, never calls a tool, and has no use for an SDK object of any kind.
Declaring `algenta-sdk` anyway, unused, would repeat exactly the leftover-placeholder-dependency
pattern an adversarial review is on record catching elsewhere in this repository's history.

## Testing this package

The conformance suite runs against a real stub HTTP server (`tests/stub_server.py`, a real FastAPI
app on a real `uvicorn` socket, never a mock) whose request/response Pydantic models mirror the
engine's public `/v1` schema field-for-field -- re-verify against your own engine version before
trusting it blindly if it's been a while. Every test drives the real,
unmodified `openai` Python client (or, for the `/v1/responses` SSE event-sequence assertions,
real `httpx` directly against the raw event stream) -- never a mocked client.

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest vllm-algenta -v
```

`tests/test_chat_completions_matrix.py` covers the `/v1/chat/completions` matrix named in this
package's own scope: a basic completion, the deterministic default model's always-`"stop"`
`finish_reason`, synthetic rechunked streaming, a real tool-calling round trip (`tool_calls`
returned, `finish_reason="tool_calls"`) on a tool-capable model, the default model's loud rejection
of a `tools=` argument it can't honor, and the current streaming+tool-calling combination refusal.
`tests/test_responses_endpoint.py` covers the `/v1/responses` envelope this package's stub
currently implements -- see the capability table above for the honest gap between that and what
the real engine's `/v1/responses` surface supports today.
`tests/test_client_config.py` is fast, no-network unit coverage of `vllm_algenta.client`'s own
`base_url`/`api_key` resolution logic.
