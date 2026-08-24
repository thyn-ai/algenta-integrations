# pydantic-ai-algenta

pydantic-ai tool integration for [Algenta](https://algenta.ai): `AlgentaToolset`, a
[`WrapperToolset`](https://ai.pydantic.dev/toolsets/#wrapping-a-toolset) that wraps a
[`pydantic_ai.mcp.MCPToolset`](https://ai.pydantic.dev/mcp/client/) pointed at your own
self-hosted Algenta Engine, and layers on:

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **A typed `execute_decision` result** -- `execute_decision`'s result is parsed into an
  [`ExecutionReceipt`][receipt] on success, so your code gets a typed object instead of an
  untyped dict. Every other tool's result (`plan_decision`, `log_decision`, `get_contract`, ...)
  is freeform and passes through unchanged -- there is no shared envelope every tool returns.
- **Native denial handling** -- `execute_decision` is fully synchronous: a call either succeeds
  or is blocked in the very same call by one of three named policy gates. A blocked call
  surfaces through pydantic-ai's own denial primitive
  ([`ToolDenied`](https://ai.pydantic.dev/api/tools/#pydantic_ai.tools.ToolDenied), the same
  thing a human reviewer's "no" produces) with the real gate name preserved -- not a bespoke
  mechanism, and not pydantic-ai's deferred-tool-*approval* primitives, since there is nothing
  asynchronous to pause on. See [The execute_decision result](#the-execute_decision-result)
  below.

[receipt]: ./pydantic_ai_algenta/receipts.py

## Install

```bash
pip install pydantic-ai-algenta
```

This package depends on exactly two things: the published
[`algenta-sdk`](https://pypi.org/project/algenta-sdk/) and
[`pydantic-ai-slim[mcp]`](https://pypi.org/project/pydantic-ai-slim/) (which pulls in
[`fastmcp`](https://gofastmcp.com)'s client, since that's what pydantic-ai's own MCP support is
built on). It never depends on, imports, or bundles any part of the Algenta Engine itself.

## Self-hosted-first

`AlgentaToolset` talks to **your own self-hosted Algenta Engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the constructor,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```python
from pydantic_ai import Agent
from pydantic_ai_algenta import AlgentaToolset

# Talks to your own self-hosted engine (ALGENTA_BASE_URL, or the constructor arg below).
toolset = AlgentaToolset(base_url="http://localhost:8000/mcp", profile="observe")

agent = Agent("openai:gpt-5", toolsets=[toolset])

result = await agent.run("What's the expected value of scenario X?")
print(result.output)
```

`profile="observe"` is also the default if you omit it -- the agent can call
`get_contract` / `query_data` / `simulate` / `recommend`, and nothing that plans, logs, or
executes anything. See [Tool profiles](#tool-profiles) to opt into more.

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
`AlgentaToolset.call_tool` maps whichever one comes back onto pydantic-ai's own primitives:

- **Success** -- the result validates as an [`ExecutionReceipt`][receipt] and is returned as-is:

  ```python
  from pydantic_ai_algenta import ExecutionReceipt

  receipt: ExecutionReceipt = tool_return_part.content
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
  gates, and `AlgentaToolset.call_tool` returns
  [`ToolDenied`](https://ai.pydantic.dev/api/tools/#pydantic_ai.tools.ToolDenied) (which
  pydantic-ai turns into `ToolReturnPart(outcome="denied")`), with the real gate name and the
  engine's own message/override hint preserved in the denial message:

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
  is not this package's concern to map: it already surfaces as whatever pydantic-ai's own
  `MCPToolset` raises (typically
  [`ToolFailed`](https://ai.pydantic.dev/api/exceptions/#pydantic_ai.exceptions.ToolFailed) or
  [`ModelRetry`](https://ai.pydantic.dev/api/exceptions/#pydantic_ai.exceptions.ModelRetry))
  before `AlgentaToolset.call_tool` ever gets a result to parse.

### Why not `ApprovalRequired` / `DeferredToolRequests`?

An earlier version of this package modeled `execute_decision` as pausing for an out-of-band
approval, surfacing that pause through pydantic-ai's deferred-tool-approval primitives
(`ApprovalRequired` -> `DeferredToolRequests` -> `DeferredToolResults`). That was wrong: checked
directly against the real engine, `execute_decision` has no asynchronous "pending" state at
all -- a call either succeeds or is blocked by a named gate in the very same call, and the
engine's separate, genuine human-approval system for decision plans is explicitly not exposed as
an MCP tool. There is nothing for this package to pause on, so this version removes that
machinery entirely rather than keep it dormant for a case that cannot occur. A blocked
`execute_decision` call is a deliberate "no" decided synchronously by the engine, and
`ToolDenied` -- pydantic-ai's own primitive for exactly that -- is the correct, and simpler,
fit.

## Typed receipts

Every tool other than `execute_decision` returns its own freeform result and passes through
`AlgentaToolset` unchanged -- there is no shared "governed execution" envelope every tool
returns. `execute_decision`'s successful result is the one exception, always parsed into an
[`ExecutionReceipt`][receipt]; see [The execute_decision result](#the-execute_decision-result)
above.

## Testing this package's own test suite (not your agent)

The test suite (`tests/`) runs a real
[`fastmcp.FastMCP`](https://gofastmcp.com) server over a real local HTTP socket -- a
deliberately fake, deterministic stand-in for a self-hosted Algenta MCP endpoint, never a real
engine (none is reachable in CI) -- and drives it with a real `AlgentaToolset` /
`pydantic_ai.mcp.MCPToolset` / `pydantic_ai.Agent`, using
[`TestModel`](https://ai.pydantic.dev/api/models/test/) to script tool-calling deterministically.
`ALLOW_MODEL_REQUESTS = False` is set in `tests/conftest.py` as a real, enforced guard against a
test accidentally calling a live model.

```bash
cd python
uv sync --all-packages --all-extras
uv run --package pydantic-ai-algenta pytest pydantic-ai-algenta/tests -v
```

(`fastmcp`'s full/server-side package and `anyio`'s pytest plugin are `dev`-only extras of this
package -- neither is a runtime dependency of `AlgentaToolset` itself.)
