# google-adk-algenta

[![PyPI](https://img.shields.io/pypi/v/google-adk-algenta.svg)](https://pypi.org/project/google-adk-algenta/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../README.md)

Google Agent Development Kit (ADK) tool integration for [Algenta](https://algenta.ai):
`AlgentaToolset`, a governed-execution-aware wrapper around ADK's own `McpToolset`
pointed at your own self-hosted Algenta engine, and layers on:

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **A typed `execute_decision` result** -- `execute_decision`'s result is parsed into an
  [`ExecutionReceipt`][receipt] on success, so your code gets a typed object instead of an
  untyped dict. Every other tool's result (`plan_decision`, `log_decision`, `get_contract`, ...)
  is freeform and passes through unchanged -- there is no shared envelope every tool returns.
- **Native denial handling** -- `execute_decision` is fully synchronous: a call either succeeds
  or is blocked in the very same call by one of three named policy gates. A blocked call raises
  [`AlgentaExecutionBlocked`][blocked] with the real gate name and the engine's own
  message/override hint preserved. There is no "pending approval" state to model.

[receipt]: ./google_adk_algenta/receipts.py
[blocked]: ./google_adk_algenta/exceptions.py

## Install

```bash
pip install google-adk-algenta
```

This package depends on the published [`algenta-sdk`](https://pypi.org/project/algenta-sdk/),
[`google-adk`](https://pypi.org/project/google-adk/), and the `mcp` package that ADK's MCP
tool support imports at runtime. It never depends on, imports, or bundles any part of the Algenta
engine itself.

## Self-hosted-first

`AlgentaToolset` talks to **your own self-hosted Algenta engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the constructor,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

**Prerequisites:**

- A running self-hosted Algenta engine, reachable over MCP -- defaults to
  `http://localhost:8000/mcp`; point elsewhere via `ALGENTA_BASE_URL` or the constructor's
  `base_url=`. No Algenta account or Algenta-issued API key is ever needed: Algenta isn't a
  hosted service you sign up for.
- An API key for whichever model you pass to `LlmAgent(...)` -- the example below uses
  `"gemini-2.5-pro"`, which needs `GOOGLE_API_KEY` set in your environment.

Don't have a self-hosted engine running yet? [Try it locally](#try-it-locally-no-live-engine-required)
below runs the same toolset end to end with neither an engine nor a model API key.

```python
import asyncio

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google_adk_algenta import AlgentaToolset


async def main() -> None:
    # Talks to your own self-hosted engine (ALGENTA_BASE_URL, or the constructor arg below).
    toolset = AlgentaToolset(base_url="http://localhost:8000/mcp", profile="observe")
    agent = LlmAgent(
        name="algenta_agent",
        model="gemini-2.5-pro",
        instruction="You are a helpful assistant with access to Algenta tools.",
        tools=[toolset],
    )
    runner = Runner(
        app_name="algenta_demo",
        agent=agent,
        session_service=InMemorySessionService(),
    )
    session = runner.session_service.create_session_sync(
        app_name="algenta_demo", user_id="demo_user"
    )

    result = await runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message="What's the expected value of scenario X?",
    )
    async for event in result:
        if event.content and event.content.parts:
            print(event.content.parts[0].text)


asyncio.run(main())
```

`profile="observe"` is also the default if you omit it -- the agent can call
`get_contract` / `query_data` / `simulate` / `recommend`, and nothing that plans, logs, or
executes anything. See [Tool profiles](#tool-profiles) to opt into more.

## Try it locally (no live engine required)

This repository's test suite includes a real stub Algenta MCP server
([`tests/stub_server.py`](./tests/stub_server.py)) -- a genuine
[`fastmcp.FastMCP`](https://gofastmcp.com) server over a real local HTTP socket, not a mock.
[`local_demo.py`](./local_demo.py) is a short, real script that starts the stub server, points
an `AlgentaToolset` at it, and calls one tool directly so you can see a real result with
**no self-hosted engine and no model API key**:

```bash
git clone https://github.com/thyn-ai/algenta-integrations
cd algenta-integrations/python
uv sync --package google-adk-algenta --all-extras
uv run --package google-adk-algenta python google-adk-algenta/local_demo.py
```

`local_demo.py` isn't part of the published `google-adk-algenta` PyPI package -- it imports
`tests.stub_server`, which only exists in a checkout of this repository.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution -- see below. |
| `full` | everything the connected engine advertises | Opt-in only; admin/ops tooling. |

```python
toolset = AlgentaToolset(base_url="...", profile="execute")
```

An `observe`-profile toolset's `get_tools()` genuinely does not list `execute_decision` (or
anything `govern`/`execute`-tier) -- it's not just undocumented, the model has no way to know it
exists. `force` / `override_safety` (operator/break-glass-only fields on `execute_decision`'s
real schema) are never exposed either, in any profile: stripped from the advertised JSON schema
*and* scrubbed from the arguments dict actually forwarded to the wrapped MCP call, in case
something upstream still tried to pass one.

## The decision lifecycle

The real lifecycle behind the `govern`/`execute` profiles is: `plan_decision(...)` produces a
freeform, not-yet-committed plan summary; `log_decision(chosen_action, ...)` persists a decision
record and returns its `decision_id`; `execute_decision(decision_id, webhook_url, ...)` dispatches
that already-logged decision for real-world execution (a webhook delivery) and returns an
execution receipt. There is no separate, model-reachable approval step in between -- a genuinely
separate human-approval system exists on the engine side (plan/case/analysis-run review), but the
engine's own MCP tool registry does not expose it as a tool at all, by design, so no integration
package -- this one included -- can wire up a flow around it.

## The `execute_decision` result

`execute_decision` is real-world execution, and it is fully synchronous: **every call returns
either a success or a named denial in that same call** -- never "pending, check back later".
`AlgentaToolset` maps whichever one comes back onto ADK's own primitives:

- **Success** -- the result validates as an [`ExecutionReceipt`][receipt] and is returned as-is:

  ```python
  from google_adk_algenta import ExecutionReceipt

  receipt: ExecutionReceipt = tool_result
  receipt.decision_id
  receipt.webhook_url
  receipt.execution_status     # "delivered" | "failed"
  receipt.response_code
  receipt.safety_overridden    # True if a human operator's force/override_safety applied
  ```

  Note that `execution_status == "failed"` -- the downstream webhook delivery itself failed --
  is still a *successful* `execute_decision` call. The engine did what was asked and is
  honestly reporting the outcome; it isn't refusing the call, so this is not a denial.

- **Denial** -- the engine synchronously blocks the call with one of exactly three named policy
  gates, and `AlgentaToolset` raises
  [`AlgentaExecutionBlocked`][blocked] with the real gate name and the engine's own message and
  override hint:

  | Gate | Meaning | Bypass |
  |---|---|---|
  | `idempotency` | This `decision_id` has already been delivered. | `force=true`, for one re-execution. Operator-only; never model-facing. |
  | `confidence` | The decision's confidence is below `policy.min_confidence`. | `override_safety=true`. Operator-only; never model-facing. |
  | `risk_floor` | `risk_p5` is below `-policy.risk_floor`. | `override_safety=true`. Operator-only; never model-facing. |

  A human operator applying one of those bypasses does so outside the model-facing tool call
  entirely (their own direct call to the engine, or a break-glass path in your own code) --
  never by the model setting `force`/`override_safety` itself, which is exactly what the
  never-model-facing scrubbing above prevents.

- **Anything else** -- a transport/HTTP-level failure (a dropped connection, a 5xx, a timeout)
  surfaces as whatever ADK's own `McpToolset` raises, before `AlgentaToolset` ever gets a result
  to parse.

### Why not a "pending approval" state?

An earlier mental model treated `execute_decision` as pausing for an out-of-band approval. That
was wrong: checked directly against the real engine, `execute_decision` has no asynchronous
"pending" state at all -- a call either succeeds or is blocked by a named gate in the very same
call, and the engine's separate, genuine human-approval system for decision plans is explicitly
not exposed as an MCP tool. There is nothing for this package to pause on, so it doesn't. A
blocked `execute_decision` call is a deliberate "no" decided synchronously by the engine, and a
catchable `AlgentaExecutionBlocked` exception is the correct, simpler fit.

## Typed receipts

Every tool other than `execute_decision` returns its own freeform result and passes through
`AlgentaToolset` unchanged -- there is no shared "governed execution" envelope every tool
returns. `execute_decision`'s successful result is the one exception, always parsed into an
[`ExecutionReceipt`][receipt]; see [The execute_decision result](#the-execute_decision-result)
above.

## Testing this package's own test suite (not your agent)

The test suite (`tests/`) runs a real [`fastmcp.FastMCP`](https://gofastmcp.com) server over a
real local HTTP socket -- a deliberately fake, deterministic stand-in for a self-hosted Algenta
MCP endpoint, never a real engine (none is reachable in CI) -- and drives it with a real
`AlgentaToolset` / ADK `McpToolset`, using direct tool calls to keep the tests fast and fully
deterministic.

```bash
cd python
uv sync --all-packages --all-extras
uv run --package google-adk-algenta pytest google-adk-algenta/tests -v
```

(`fastmcp`'s full/server-side package is a `dev`-only extra of this package -- neither it nor the
test helpers are runtime dependencies of `AlgentaToolset` itself.)
