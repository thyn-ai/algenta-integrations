# pydantic-ai-algenta

pydantic-ai tool integration for [Algenta](https://algenta.ai): `AlgentaToolset`, a
[`WrapperToolset`](https://ai.pydantic.dev/toolsets/#wrapping-a-toolset) that wraps a
[`pydantic_ai.mcp.MCPToolset`](https://ai.pydantic.dev/mcp/client/) pointed at your own
self-hosted Algenta Engine, and layers on:

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **Typed governed-execution receipts** -- every tool call's result is parsed into a
  [`GovernedExecutionReceipt`][receipt] when it validates as one, so your code gets a typed
  object instead of an untyped dict.
- **Native approval handling** -- a paused, approval-gated `execute_decision` call surfaces
  through pydantic-ai's own
  [deferred-tool-approval](https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval)
  primitives (`ApprovalRequired` / `DeferredToolRequests` / `DeferredToolResults`) -- not a
  bespoke mechanism -- with a thin `approve_and_resume` convenience over the resume step.

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
`get_contract` / `query_data` / `simulate` / `recommend`, and nothing that writes, plans, or
executes anything. See [Tool profiles](#tool-profiles) to opt into more.

Every tool call's result comes back on `ToolReturnPart.content` as a typed
[`GovernedExecutionReceipt`][receipt] (when the connected tool returns Algenta's governed-execution
envelope -- see [Typed receipts](#typed-receipts)), so downstream code can do
`receipt.result`, `receipt.approval_state`, etc. instead of indexing into a raw dict.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution, approval-gated -- see below. |
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

## The approval flow

`execute_decision` is real-world execution and is approval-gated. When the connected engine's
result envelope reports `approval_state == "pending"`, `AlgentaToolset.call_tool` raises
[`ApprovalRequired`][ApprovalRequired] instead of returning a normal result -- which ends the
agent run early with a `DeferredToolRequests` as its output, carrying everything you need to act
on the pending plan in `metadata` (`plan_hash`, `execution_id`, `idempotency_key`, and the full
receipt):

```python
from pydantic_ai.tools import DeferredToolRequests

result = await agent.run(
    "execute the approved restock plan",
    output_type=[str, DeferredToolRequests],  # required for a paused run to surface cleanly
)

if isinstance(result.output, DeferredToolRequests):
    call_id = result.output.approvals[0].tool_call_id
    metadata = result.output.metadata[call_id]
    # metadata["plan_hash"], metadata["execution_id"], metadata["idempotency_key"], ...
    ...  # go get a human (or your own policy engine) to actually approve metadata["plan_hash"]
```

Once the plan is genuinely approved on the engine side, resume with `approve_and_resume`, which
collapses "call your approval endpoint" + "build `DeferredToolResults`" + "resume the run" into
one call:

```python
from pydantic_ai_algenta import approve_and_resume

async def approve(metadata: dict) -> None:
    # Call *your* engine's real approval endpoint here -- see "Why `approve` is a callback"
    # below for why this package doesn't hardcode that call.
    await my_algenta_sdk_client.approve_agent_run(metadata["execution_id"])

resumed = await approve_and_resume(
    agent,
    message_history=result.all_messages(),
    deferred_requests=result.output,
    approve=approve,
)
print(resumed.output)
```

If a human instead **denies** the request, resolve it with pydantic-ai's own primitive directly
(no helper needed -- `AlgentaToolset` never re-calls the engine for a denial; pydantic-ai
resolves it entirely on its own):

```python
from pydantic_ai.tools import DeferredToolResults

results = DeferredToolResults()
results.approvals[call_id] = False  # or ToolDenied(message="...") for a custom message
denied = await agent.run(message_history=result.all_messages(), deferred_tool_results=results)
```

A call the engine denies outright -- a named policy-gate `code` such as `plan_hash_mismatch`,
`stale_plan`, `plan_not_approved`, or `idempotency_key_conflict`, or `approval_state in
("rejected", "expired")` -- never goes through the approval flow at all: it comes back as an
ordinary `ToolReturnPart(outcome="denied")` with the engine's own code/message preserved, so the
model sees it in the same step rather than waiting on a resume.

[ApprovalRequired]: https://ai.pydantic.dev/api/exceptions/#pydantic_ai.exceptions.ApprovalRequired

### Why `approve` is a callback, not a hardcoded SDK call

`approve_and_resume` takes your approval call as a callback (`approve: Callable[[dict],
Awaitable[Any] | Any]`) rather than calling a specific `algenta-sdk` method itself. This was a
deliberate choice made after actually checking: the published `algenta-sdk` (PyPI, version
1.0.11, checked directly against its installed source while building this package -- the one
thing this package is allowed to depend on) does not expose a `decision_plans.approve(plan_id)`
or `execution.approve(execution_id)`-shaped method keyed the way this envelope's `plan_hash` /
`execution_id` fields are. Its nearest real analog is the differently-shaped, run-id-keyed
`client.approve_agent_run(run_id)`. Hardcoding a call to a method that doesn't exist on the real,
published client would be worse than not calling it at all, so `approve_and_resume` leaves that
one call to you -- and will pick up a better-matching SDK method transparently, with no change to
this function, the moment one ships.

## Typed receipts

Every governed Algenta MCP tool call's result is parsed into a `GovernedExecutionReceipt`:

```python
from pydantic_ai_algenta import GovernedExecutionReceipt

receipt: GovernedExecutionReceipt = tool_return_part.content
receipt.status            # "ok" | "error" | ...
receipt.code               # "ok" | "plan_hash_mismatch" | "upstream_timeout" | ...
receipt.approval_state     # "none" | "pending" | "approved" | "rejected" | "expired"
receipt.plan_hash
receipt.execution_id
receipt.idempotency_key
receipt.result              # the tool's actual payload, once unwrapped from the envelope
```

A tool whose result *doesn't* validate as this envelope (e.g. a real `get_contract`'s discovery
payload) passes through unchanged as an ordinary result -- `AlgentaToolset` doesn't assume every
tool on a self-hosted Algenta MCP endpoint returns this exact shape, only that governed
decision/execution tools do.

### Why not `ToolReturnPart(outcome="interrupted")` for a paused execution?

If you've read pydantic-ai's `ToolReturnPart.outcome` docs, you might expect a paused,
approval-gated call to come back with `outcome="interrupted"`. It doesn't, on purpose, once you
read what `"interrupted"` actually means in pydantic-ai 2.33.0: it's synthesized by the framework
itself for a genuinely interrupted run (a crash, a dropped stream, a cancellation) during message-
history repair -- never something a tool's own return value can request. A paused governed
execution isn't an interruption; it's a deliberate, resumable pause with its own dedicated
primitive (`ApprovalRequired` -> `DeferredToolRequests`), which is what `AlgentaToolset.call_tool`
raises instead. `success` / `denied` / `failed` do map onto `outcome` exactly as you'd expect
(returning the receipt, returning `ToolDenied(...)`, and raising `ToolFailed(...)`,
respectively).

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
