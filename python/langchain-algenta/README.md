# langchain-algenta

LangChain / LangGraph tool integration for [Algenta](https://algenta.ai): `create_algenta_tools`,
a function that builds a governed-execution-aware list of
[`BaseTool`](https://python.langchain.com/api_reference/core/tools/langchain_core.tools.base.BaseTool.html)s
from your own self-hosted Algenta Engine's MCP tool surface, and layers on:

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **Typed execution receipts and denials** -- a successful `execute_decision` call is parseable
  into an [`ExecutionReceipt`][receipt] via `parse_receipt`; a blocked one raises
  `AlgentaExecutionBlocked`, carrying the engine's own named gate.
- **A real, synchronous denial mapping** -- `execute_decision` either succeeds or is blocked by
  one of exactly three named policy gates, decided in the *same call* -- never a separate
  "pending approval" step. A blocked call surfaces as a normal, catchable LangChain tool-call
  error, not a paused run.

[receipt]: ./langchain_algenta/receipts.py

## Install

```bash
pip install langchain-algenta
```

This package depends on the published [`algenta-sdk`](https://pypi.org/project/algenta-sdk/)
(the only Algenta-owned dependency any package in this repository may declare) plus two real,
non-optional runtime dependencies: `langchain-core` and `langchain-mcp-adapters`. `langgraph` is
**not** a runtime dependency of this package -- see [Why no `langgraph`
dependency](#why-no-langgraph-dependency-honest-history) for why that's worth calling out
explicitly. The tools `create_algenta_tools` returns are ordinary LangChain `BaseTool`s, fully
usable inside a `langgraph` agent if that's how you build yours -- that's your own dependency to
add, not this package's.

## Self-hosted-first

`create_algenta_tools` talks to **your own self-hosted Algenta Engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the function,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```python
from langchain.agents import create_agent
from langchain_algenta import AlgentaExecutionBlocked, create_algenta_tools

# Talks to your own self-hosted engine (ALGENTA_BASE_URL, or the base_url= arg below).
tools = await create_algenta_tools(base_url="http://localhost:8000/mcp", profile="execute")

agent = create_agent("openai:gpt-5", tools=tools)

try:
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Execute the decision we just logged."}]}
    )
    print(result["messages"][-1].content)
except AlgentaExecutionBlocked as blocked:
    # blocked.gate is one of "idempotency" | "confidence" | "risk_floor"
    print(f"execute_decision was blocked by the {blocked.gate!r} gate: {blocked.denial.message}")
```

`profile="observe"` is the default if you omit it -- the agent can call
`get_contract` / `query_data` / `simulate` / `recommend`, and nothing that writes, plans, or
executes anything. See [Tool profiles](#tool-profiles) to opt into more.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution -- see below for the denial model. |
| `full` | everything the connected engine advertises | Opt-in only; admin/ops tooling. |

```python
tools = await create_algenta_tools(base_url="...", profile="execute")
```

An `observe`-profile call to `create_algenta_tools` genuinely does not return `execute_decision`
(or anything `govern`/`execute`-tier) -- it's not just undocumented, the model has no way to know
it exists. As defense-in-depth, `AlgentaToolCallInterceptor` (the mechanism described below) also
refuses to actually call a tool outside the active profile even if something bypassed the
filtered list -- e.g. by calling `langchain_mcp_adapters`' own `client.get_tools()` directly on a
client this package built.

`force` / `override_safety` (operator/break-glass-only fields on `execute_decision`'s real
schema) are never exposed either, in any profile: stripped from every returned tool's advertised
JSON schema *and* scrubbed from the arguments dict actually forwarded to the wrapped MCP call, in
case something upstream still tried to pass one.

## How this is built: an MCP tool-call interceptor, not a re-wrapped tool

Unlike this repository's `pydantic-ai-algenta` and `algenta-tools` (Vercel AI SDK) siblings --
which both wrap each tool object's own call method directly, because that's the natural seam in
those frameworks -- `langchain-algenta` is built on
[`langchain_mcp_adapters`'s `ToolCallInterceptor`](https://github.com/langchain-ai/langchain-mcp-adapters)
protocol instead. `create_algenta_tools` builds a
`langchain_mcp_adapters.client.MultiServerMCPClient` with an `AlgentaToolCallInterceptor`
registered in `tool_interceptors=`; every real tool call that client's tools make is routed
through it before the real MCP network call happens.

Tool-*listing* (which names even get returned) is unaffected by interceptors -- they only fire on
a call, not on `get_tools()` -- so profile filtering and the schema-level `force`/
`override_safety` scrub still happen as a plain post-processing pass over the tool list, the same
shape as the pydantic-ai and TypeScript siblings do it.

The `tools=` escape hatch (an in-memory list of `BaseTool`s -- a fake registry in a test, or your
own pre-built tools with no MCP client behind them at all) has no MCP client to attach an
interceptor to, so it rebuilds each tool's coroutine directly instead, sharing the exact same
denial-mapping logic (`langchain_algenta.governance.resolve_governed_call`).

## The real `execute_decision` denial model

`execute_decision` takes `decision_id` and `webhook_url` (plus the operator-only `force` /
`override_safety`, and an optional `timeout_seconds`/`metadata`) and dispatches an
already-planned, already-logged decision for real-world execution. It has exactly two outcomes,
decided **synchronously, in the same call**:

1. **Success** -- a real `ExecutionReceipt`: `{decision_id, webhook_url, execution_status,
   response_code, executed_at, policy_snapshot_id, schema_snapshot_id, manifest_version,
   payload_summary, safety_overridden}`. `execution_status` can be `"delivered"` or `"failed"` --
   `"failed"` means the webhook target itself rejected delivery; the call still completed and
   this is still a success from `execute_decision`'s own point of view, not a denial.
2. **Blocked** -- the engine's `409` response, reporting exactly one of three real, named policy
   gates:
   - `"idempotency"`: this `decision_id` was already delivered. `force=true` bypasses *only*
     this gate, and only for one re-execution.
   - `"confidence"`: the logged decision's confidence is below `policy.min_confidence`.
     Bypassable only via `override_safety=true`.
   - `"risk_floor"`: the logged decision's `risk_p5` is below `-policy.risk_floor`. Bypassable
     only via `override_safety=true`.

`AlgentaToolCallInterceptor` maps a blocked call onto `AlgentaExecutionBlocked` -- a plain
exception (deliberately *not* a `langchain_core.tools.ToolException`; see [Why plain exceptions,
not `ToolException`](#why-plain-exceptions-not-toolexception) below) carrying the parsed
`ExecutionDenial` on `.denial` (`.denial.gate`, `.denial.code`, `.denial.message`,
`.denial.override_hint`), with `.gate` as a shortcut onto `.denial.gate`:

```python
from langchain_algenta import AlgentaExecutionBlocked

try:
    result = await execute_decision.ainvoke({"decision_id": "...", "webhook_url": "..."})
except AlgentaExecutionBlocked as blocked:
    if blocked.gate == "idempotency":
        ...  # already delivered; decide whether a real re-execution is actually warranted
    elif blocked.gate in ("confidence", "risk_floor"):
        ...  # policy said no; this is not something to silently retry
```

An error that *isn't* one of these three recognized gates (a generic transport failure, or some
future error shape this package doesn't know about yet) is deliberately **not** wrapped in an
Algenta-specific exception -- it's left to `langchain_mcp_adapters`' own, already-correct handling
of a generic MCP tool execution error, the native framework idiom for "this tool call failed"
that this package has no reason to shadow when it isn't one of the three specific gates it
actually understands.

**There is no third, "pending" outcome, and nothing here to pause on.** A genuinely separate,
`plan_hash`+nonce human-approval system does exist on the real engine, but its own source says
explicitly that it is intentionally not exposed as an MCP/LLM tool -- no MCP-based integration
package, this one included, can ever observe or wait on it. `execute_decision` itself commits to
an answer -- success or one of the three named gates -- in the one call you make.

### Why plain exceptions, not `ToolException`

`langchain_core.tools.ToolException` is LangChain's own idiom for "let the agent see this failure
and try to self-correct" -- it gets swallowed by `BaseTool`'s `handle_tool_error` machinery into
an error-status `ToolMessage` by default, rather than propagating. `AlgentaExecutionBlocked` (and
`AlgentaToolDenied`, the unrelated profile-violation guard) are deliberately *not*
`ToolException` subclasses, so they propagate to your own calling code unmodified -- a policy
denial on real-world execution is not something this package wants silently absorbed into a chat
message by default; you decide what an agent should be told about it, if anything.

### Why no `langgraph` dependency (honest history)

An earlier version of this package called `langgraph.types.interrupt(...)` unconditionally
whenever the connected engine reported a fictional `approval_state == "pending"` result --
`execute_decision` never actually returns that; there is no such field and no such state on the
real tool at all. Once that pause (and the retry-after-resume machinery built around it) was
removed as dead, fictional code, nothing in this package's own source imports `langgraph`
anymore, so it was dropped from `dependencies` too. This is a genuine simplification, not a
missing feature: the real tool has strictly less state to reason about than the fictional one
did.

## Typed receipts and denials

A successful `execute_decision` call's result payload is parseable into an `ExecutionReceipt`:

```python
from langchain_algenta import ExecutionReceipt, parse_receipt

# tool_message.content is a list of LangChain content blocks; the JSON payload is the first
# text block's text, same as what a real chat model would see.
import json
payload = json.loads(tool_message.content[0]["text"])
receipt: ExecutionReceipt | None = parse_receipt(payload)
if receipt is not None:
    receipt.decision_id
    receipt.webhook_url
    receipt.execution_status     # "delivered" | "failed"
    receipt.is_delivered()       # execution_status == "delivered"
    receipt.response_code
    receipt.safety_overridden    # True if override_safety was needed to get here
    receipt.payload_summary      # what was actually delivered to webhook_url
```

`parse_receipt` returns `None` for any other tool's own result shape -- `get_contract`'s
discovery payload, `log_decision`'s `{decision_id, chosen_action, expected_value, confidence,
created_at, note}`, `plan_decision`'s plan summary -- none of those are `execute_decision`
receipts, and this package never pretends they are.

### Why no typed receipt on the tool's return value

`pydantic-ai-algenta` and `algenta-tools` (Vercel AI SDK) both return the *parsed*
`ExecutionReceipt` object as the tool call's actual return value -- their frameworks let a
wrapped tool call return anything. `langchain-algenta` can't do the same thing from inside
`AlgentaToolCallInterceptor`: `langchain_mcp_adapters.interceptors.ToolCallInterceptor` is typed to
return `CallToolResult | ToolMessage | Command`, not an arbitrary Python object, and returning
anything else there would fight the framework's own downstream content conversion rather than
cooperate with it. So on the success/passthrough path, this package deliberately returns the
underlying `CallToolResult` completely unchanged, and leaves receipt parsing to you (via
`parse_receipt`, as shown above) -- an honest difference in shape from its siblings, not an
oversight.

## Testing this package's own test suite (not your agent)

The test suite (`tests/`) runs a real
[`mcp.server.fastmcp.FastMCP`](https://modelcontextprotocol.io/) server (the base MCP SDK's own
FastMCP -- already a transitive dependency of `langchain-mcp-adapters`, so no extra `fastmcp`
package is needed) over a real local HTTP socket -- a deliberately fake, deterministic stand-in
for a self-hosted Algenta MCP endpoint, never a real engine (none is reachable in CI) -- and
drives it with the real `create_algenta_tools` / `MultiServerMCPClient` /
`AlgentaToolCallInterceptor` round trip, including all three real named policy gates on
`execute_decision`.

```bash
cd python
uv sync --all-packages --all-extras
uv run --package langchain-algenta pytest langchain-algenta/tests -v
```
