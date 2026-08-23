# langchain-algenta

LangChain / LangGraph tool integration for [Algenta](https://algenta.ai): `create_algenta_tools`,
a function that builds a governed-execution-aware list of
[`BaseTool`](https://python.langchain.com/api_reference/core/tools/langchain_core.tools.base.BaseTool.html)s
from your own self-hosted Algenta Engine's MCP tool surface, and layers on:

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **Typed governed-execution receipts** -- every tool call's result is parseable into a
  [`GovernedExecutionReceipt`][receipt] via `parse_receipt`, so your code gets a typed object
  instead of hand-parsing a raw dict.
- **Native approval handling** -- a paused, approval-gated `execute_decision` call surfaces
  through LangGraph's own
  [`interrupt()`](https://langchain-ai.github.io/langgraph/how-tos/human_in_the_loop/)
  human-in-the-loop primitive when your agent has a checkpointer -- not a bespoke mechanism --
  with an honest accounting below of exactly when that's true and what happens when it isn't.

[receipt]: ./langchain_algenta/receipts.py

## Install

```bash
pip install langchain-algenta
```

This package depends on the published [`algenta-sdk`](https://pypi.org/project/algenta-sdk/)
(the only Algenta-owned dependency any package in this repository may declare) plus three real,
non-optional runtime dependencies: `langchain-core`, `langchain-mcp-adapters`, and `langgraph`.
`langgraph` is a hard dependency, not an optional extra, even if you never build a `StateGraph`
by hand -- see [Why `interrupt()`](#why-interrupt-and-what-happens-after-resume) for why.

## Self-hosted-first

`create_algenta_tools` talks to **your own self-hosted Algenta Engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the function,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```python
from langchain.agents import create_agent
from langgraph.checkpoint.memory import InMemorySaver
from langchain_algenta import create_algenta_tools

# Talks to your own self-hosted engine (ALGENTA_BASE_URL, or the base_url= arg below).
tools = await create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")

agent = create_agent("openai:gpt-5", tools=tools, checkpointer=InMemorySaver())

result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "What's the expected value of scenario X?"}]},
    {"configurable": {"thread_id": "session-1"}},
)
print(result["messages"][-1].content)
```

`profile="observe"` is also the default if you omit it -- the agent can call
`get_contract` / `query_data` / `simulate` / `recommend`, and nothing that writes, plans, or
executes anything. See [Tool profiles](#tool-profiles) to opt into more.

`checkpointer=InMemorySaver()` (or any real `BaseCheckpointSaver`) is what makes a paused
`execute_decision` call genuinely resumable rather than a dead end -- see the approval section
below.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution, approval-gated -- see below. |
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
through it before the real MCP network call happens, and the interceptor's `handler` can be
called more than once per call -- which is exactly what makes the approval retry below possible
without any bespoke plumbing.

Tool-*listing* (which names even get returned) is unaffected by interceptors -- they only fire on
a call, not on `get_tools()` -- so profile filtering and the schema-level `force`/
`override_safety` scrub still happen as a plain post-processing pass over the tool list, the same
shape as the pydantic-ai and TypeScript siblings do it.

The `tools=` escape hatch (an in-memory list of `BaseTool`s -- a fake registry in a test, or your
own pre-built tools with no MCP client behind them at all) has no MCP client to attach an
interceptor to, so it rebuilds each tool's coroutine directly instead, sharing the exact same
approval-mapping logic (`langchain_algenta.governance.resolve_governed_call`).

## Why `interrupt()`, and what happens after resume

`execute_decision` is real-world execution and is approval-gated. When the connected engine's
result envelope reports `approval_state == "pending"`, `AlgentaToolCallInterceptor` calls
[`langgraph.types.interrupt(...)`](https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/)
with the plan's identifying fields (`plan_hash`, `execution_id`, `idempotency_key`, and the full
receipt) as its payload.

**This is a genuine, framework-native mid-call pause -- not a documentation claim.** Verified
directly against the installed `langgraph` 1.2.11 / `langchain-core` 1.6.0: a plain tool's own
coroutine can call `interrupt()`, and as long as the surrounding agent/graph was built with a
`checkpointer` (true for `create_agent`/`create_react_agent` the moment you pass one -- which is
the normal, current way to get resumable human-in-the-loop, not a hand-built `StateGraph`
requirement), the run genuinely suspends: `agent.ainvoke(...)` returns with `result["__interrupt__"]`
set instead of raising, and `agent.ainvoke(Command(resume=...), config)` genuinely resumes
*that exact paused tool call*.

**What resuming actually does, concretely:** `AlgentaToolCallInterceptor` doesn't try to interpret
whatever value you pass to `Command(resume=...)` as an approval decision -- there's no reliable
way to distinguish "the human approved it" from "the human said something" from inside the
interceptor. Instead, once resumed, it retries the *exact same underlying MCP call, once*, on the
theory that a human (or a policy-engine node reading `result["__interrupt__"]`) went and recorded
the real, out-of-band approval against `plan_hash` on the engine itself in between. If that retry
now comes back `approval_state="approved"` (or otherwise successful), you get that fresh result.
If it's *still* `"pending"` -- the approval genuinely wasn't recorded in time -- this package
raises `AlgentaApprovalStillPending` rather than pausing a second time: `interrupt()` is
resumable per call site within one task execution, not idempotent across repeated calls, so
pausing again here would mean a resumer who actually did approve the plan and resumed could still
get stuck forever on a slow-to-propagate approval instead of ever seeing a clear outcome.

```python
from langgraph.types import Command

result = await agent.ainvoke({"messages": [...]}, config)

if "__interrupt__" in result:
    pending = result["__interrupt__"][0].value
    # pending["plan_hash"], pending["execution_id"], pending["idempotency_key"], ...
    await my_algenta_sdk_client.approve_agent_run(pending["execution_id"])  # your real approval call
    result = await agent.ainvoke(Command(resume="approved"), config)
```

**A call the engine denies outright** -- a named policy-gate `code` such as `plan_hash_mismatch`,
`stale_plan`, `plan_not_approved`, or `idempotency_key_conflict`, or `approval_state in
("rejected", "expired")` -- never goes through `interrupt()` at all: `AlgentaToolCallInterceptor`
raises `AlgentaToolDenied` immediately, carrying the engine's own code/message on `.receipt`.
Anything else that isn't a recognized success raises `AlgentaToolExecutionFailed`.

### What `interrupt()` needs -- the honest limit

If your tool call *isn't* running inside a real LangGraph Pregel task at all (no graph, no
`create_agent`/`create_react_agent`), `langgraph.types.interrupt()` fails instead of pausing --
verified directly, not assumed, against this package's real dependency versions. The exact
exception depends on how "bare" the call is: calling a returned tool's `.ainvoke(...)` directly
(still inside *some* LangChain `Runnable` config context, since `BaseTool` is itself a
`Runnable`) gets far enough into `interrupt()` to raise `KeyError: '__pregel_scratchpad'` (it
finds a config, just not LangGraph's own Pregel-scoped scratchpad key in it); calling
`langgraph.types.interrupt()` itself with no `Runnable` context whatsoever raises the coarser
`RuntimeError("Called get_config outside of a runnable context")`. And if your graph exists but
has no `checkpointer`, the run still reports `__interrupt__` correctly, but `Command(resume=...)`
fails outright with `RuntimeError: Cannot use Command(resume=...) without checkpointer` -- also
verified directly. None of these is a limitation this package invented or can paper over: they
are exactly LangGraph's own, current, documented behavior. The only thing that changes based on
your setup is whether a pending approval becomes a graceful pause or a raised error -- this
package always calls `interrupt()` unconditionally and lets that decision fall out of whatever
context you actually run in, rather than trying to detect it ahead of time (there's no cheap way
to check "does this call have a checkpointer" from inside the interceptor) and silently
downgrading to always-raise, which would throw away this package's one real advantage over its
`pydantic-ai-algenta` and `algenta-tools` siblings.

### Why not just throw, like `algenta-tools` (Vercel AI SDK) does?

Because, unlike the AI SDK, LangGraph actually has a real primitive for this that doesn't require
throwing at all. `algenta-tools` sets `needsApproval: true` on `execute_decision` as a *pre-call*
gate (the model can't even attempt the call until an app-level approval response arrives) and
still throws if the engine reports pending *after* that gate passes, because AI SDK has no
mid-call pause primitive to fall back on. `pydantic-ai-algenta` raises `ApprovalRequired` -- a
*post-hoc* reaction after the real MCP call already ran, because pydantic-ai has no pre-call
approval gate at all. `langchain-algenta` can do better than both: because a plain tool's
coroutine can call `interrupt()` directly, mid-call, and LangGraph's own runtime turns that into a
real, resumable pause -- when a checkpointer is present, which is the normal case, not an edge
case.

## Typed receipts

Every governed Algenta MCP tool call's result payload is parseable into a
`GovernedExecutionReceipt`:

```python
from langchain_algenta import GovernedExecutionReceipt, parse_receipt

# tool_message.content is a list of LangChain content blocks; the JSON payload is the first
# text block's text, same as what a real chat model would see.
import json
payload = json.loads(tool_message.content[0]["text"])
receipt: GovernedExecutionReceipt | None = parse_receipt(payload)
if receipt is not None:
    receipt.status            # "ok" | "error" | ...
    receipt.code               # "ok" | "plan_hash_mismatch" | "upstream_timeout" | ...
    receipt.approval_state     # "none" | "pending" | "approved" | "rejected" | "expired"
    receipt.plan_hash
    receipt.execution_id
    receipt.idempotency_key
    receipt.result              # the tool's actual payload, once unwrapped from the envelope
```

### Why no typed receipt on the tool's return value?

`pydantic-ai-algenta` and `algenta-tools` (Vercel AI SDK) both return the *parsed*
`GovernedExecutionReceipt` object as the tool call's actual return value -- their frameworks let a
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
drives it with the real `create_algenta_tools` / `MultiServerMCPClient` / `AlgentaToolCallInterceptor`
round trip. The `"pending"` -> `interrupt()` -> resume path is exercised against a real, compiled
LangGraph graph with a real `InMemorySaver` checkpointer -- not just asserted to have been called.

```bash
cd python
uv sync --all-packages --all-extras
uv run --package langchain-algenta pytest langchain-algenta/tests -v
```
