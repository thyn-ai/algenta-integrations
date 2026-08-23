# llamaindex-algenta

LlamaIndex tool integration for [Algenta](https://algenta.ai): `create_algenta_tools`, a
governed-execution-aware `list[llama_index.core.tools.FunctionTool]` wrapping your own self-hosted
Algenta Engine's MCP tool surface via `llama-index-tools-mcp`'s own real
[`BasicMCPClient`](https://docs.llamaindex.ai/en/stable/api_reference/tools/mcp/) (a separate PyPI
package -- see [Why `llama-index-tools-mcp`, not the `llama-index`
metapackage](#why-llama-index-tools-mcp-not-the-llama-index-metapackage)), plus a real approval
mapping onto `llama-index-workflows`' `Context.wait_for_event()` human-in-the-loop primitive for a
paused `execute_decision` call.

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **Two-layer never-model-facing scrubbing** -- `force`/`override_safety` are stripped from every
  returned tool's advertised pydantic schema *and* from the arguments dict actually forwarded to
  the real `call_tool(...)`.
- **Typed governed-execution receipts** -- a successful governed call's `ToolOutput.raw_output` is
  the parsed `GovernedExecutionReceipt` object itself, not a stringified blob.
- **An honest approval mapping** -- see [Approval mapping](#approval-mapping) below for the full
  accounting of what LlamaIndex's real primitives do and don't guarantee, verified live against
  installed `llama-index-core` 0.14.24 / `llama-index-tools-mcp` 0.4.8 / `llama-index-workflows`
  2.23.3 / `mcp` 1.29.0, not assumed from documentation.

## Install

```bash
pip install llamaindex-algenta
```

This package depends on **`llama-index-core`** and **`llama-index-tools-mcp`** (both real,
non-optional runtime dependencies), plus a pinned **`mcp<2.0.0`** -- see [The `mcp<2.0.0`
pin](#the-mcp200-pin-a-real-currently-live-upstream-bug) -- and deliberately **not** on
`algenta-sdk` -- see [Why no `algenta-sdk`
dependency](#why-no-algenta-sdk-dependency).

## Self-hosted-first

`create_algenta_tools` talks to **your own self-hosted Algenta Engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the function,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```python
from llama_index.core.agent.workflow import FunctionAgent
from llamaindex_algenta import create_algenta_tools

tools = await create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")
agent = FunctionAgent(tools=tools, llm=your_llm)
result = await agent.run(user_msg="what should we do?")
```

`profile="observe"` is also the default if you omit it -- the agent can call `get_contract` /
`query_data` / `simulate` / `recommend`, and nothing that writes, plans, or executes anything.

`create_algenta_tools` is `async def`, matching `McpToolSpec.to_tool_list_async()`'s own real
primary interface (its `to_tool_list()` sync wrapper explicitly raises if called from inside a
running event loop -- the same caveat applies if you ever need a sync variant of this call).

There is no connection to close afterward: `BasicMCPClient`'s own real methods each open and tear
down their own MCP session per call (verified from its installed source), so this package has no
lifecycle of its own to manage either.

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

An `observe`-profile call genuinely does not return `execute_decision` (or anything
`govern`/`execute`-tier) -- the raw MCP protocol's `list_tools()` has no server-side name filter
to lean on (unlike Haystack's `MCPToolset(tool_names=...)`), so this is a single client-side
filter: a disallowed tool is never turned into a `FunctionTool` at all, and the model is never
shown its schema.

## Why this package does not just call `McpToolSpec(client=...).to_tool_list_async()`

That would be the natural-looking one-liner, and it's deliberately not what this package does.
Verified live against installed `llama-index-tools-mcp` 0.4.8: `McpToolSpec._create_tool_fn`'s
closure is

```python
async def async_tool_fn(**kwargs):
    return await self.client.call_tool(tool_name, kwargs)
```

-- no `ctx: Context` parameter anywhere. `FunctionTool.__init__` decides whether a wrapped
function needs workflow context purely by inspecting its signature for a `Context`-annotated
parameter (`requires_context`/`ctx_param_name`), so every tool `McpToolSpec` builds has
`requires_context=False` and **cannot itself call `ctx.wait_for_event()`** -- the one real,
verified-live human-in-the-loop primitive this package needs for a paused `execute_decision` (see
below). `create_algenta_tools` therefore builds its own `async def wrapper(ctx: Context, **kwargs)`
per allowed tool and hands that to `FunctionTool.from_defaults(...)` directly -- reusing only
`McpToolSpec`'s public JSON-Schema-to-pydantic-model helpers (`create_model_from_json_schema`,
`remove_model_fields`), never its tool-call closures or `fetch_tools`/`to_tool_list_async`.

**A real footgun this design decision avoids, caught by this package's own test suite while
building it:** if `llamaindex_algenta.toolset` used `from __future__ import annotations` (every
other module in this package does), `wrapper`'s `ctx: Context` annotation would become the
*string* `"Context"` at runtime instead of the real class object. `FunctionTool`'s context
detection compares the raw annotation object directly -- it never resolves postponed/string
annotations -- so `requires_context` would silently come back `False`, and every real
`FunctionAgent` call would fail with a plain `TypeError: wrapper() missing 1 required positional
argument: 'ctx'`, caught and swallowed into an ordinary tool error by the agent runtime rather than
raised anywhere visible. `llamaindex_algenta/toolset.py` deliberately omits that future import,
with a comment explaining why -- this is exactly the kind of gap only exercising the real
`FunctionAgent` path (not just inspecting the tool object) would surface.

## Approval mapping

Verified live against installed `llama-index-core` 0.14.24 / `llama-index-workflows` 2.23.3 (a
real `fastmcp.FastMCP` stub server, real HTTP wire, a real `FunctionAgent.run()` -- see
`tests/test_approval_hitl.py`), not assumed from documentation.

**The real primitive: `workflows.context.context.Context.wait_for_event()`.** Every wrapped tool
this package builds calls this itself when a receipt comes back `approval_state == "pending"`:

```python
await ctx.wait_for_event(
    HumanResponseEvent,
    waiter_id=f"algenta:{tool_name}:{plan_hash}",
    waiter_event=InputRequiredEvent(tool_name=tool_name, plan_hash=plan_hash, msg="..."),
    requirements={"plan_hash": plan_hash},
    timeout=approval_wait_timeout,  # default 2000s, matching wait_for_event's own default
)
```

This raises an internal `WaitingForEvent` control-flow exception that the workflow runtime is
documented to catch and turn into a genuine pause -- proven live through a real
`FunctionAgent.run()`: the run yields an `InputRequiredEvent` mid-tool-call, and only completes
after a caller sends back `handler.ctx.send_event(HumanResponseEvent(response="approved", ...))`.

**The one real wrinkle, and why it matters for `execute_decision` specifically:**
`Context.wait_for_event()`'s own docstring says it plainly -- *"the runtime pauses ... and replays
the entire step when the event arrives."* For a tool this package wraps, "the step" is the whole
tool-call step, MCP round trip included. On resume, the wrapped `execute_decision` call is
**redone from scratch** -- verified live in `test_approval_hitl.py` by observing that a genuinely
`approval_state="approved"` receipt is only reachable at all if the underlying `call_tool(...)`
really ran a second time (the stub server's fake plan only flips to `"approved"` after a separate,
out-of-band `_test_approve_plan` call). This mirrors LangGraph's `interrupt()` (`langchain-algenta`
documents the identical replay-the-node semantics for its own sibling design) -- but unlike that
sibling, this package needs no hand-built "retry once after resume" logic at all
(`langchain_algenta.governance.resolve_governed_call`'s explicit `retry` callback has no
counterpart here): the workflow runtime's own replay does the retry for free. This is also exactly
why the contract's `execute_decision` requiring a caller-supplied idempotency key matters in a way
it wouldn't for a tool without automatic replay-on-resume -- a real engine seeing the same call
twice (once producing "pending", once for real after approval) needs that key to treat the second
delivery correctly.

**The fail-closed fallback: `AlgentaApprovalStillPending`.** Raised only when
`wait_for_event()` itself cannot be used to pause at all:

- `ctx` isn't wired to a live, running workflow (a bare `await tool.acall(ctx=..., ...)` outside
  any `Workflow`/`FunctionAgent` run) -- `wait_for_event()` raises
  `workflows.errors.ContextStateError` in that case, and this package fails closed rather than
  proceeding as if the call had succeeded.
- The wait genuinely timed out (`asyncio.TimeoutError`) -- a real pause happened, but nobody
  resumed within `approval_wait_timeout`.

A call the engine denies outright (a named policy-gate `code` such as `plan_hash_mismatch`,
`stale_plan`, `plan_not_approved`, `idempotency_key_conflict`, or `approval_state in ("rejected",
"expired")`) raises `AlgentaToolDenied`; anything else that isn't a recognized success --
including the MCP protocol-level `isError=True` case, a server-side tool crash the MCP SDK already
turned into ordinary response data -- raises `AlgentaToolExecutionFailed`.

### Where this sits relative to the other five siblings

- `pydantic-ai-algenta` raises `ApprovalRequired` -- a **post-hoc** reaction after the real MCP
  call already happened, because pydantic-ai has no pre-call approval gate at all.
- `algenta-tools` (Vercel AI SDK) sets `needsApproval: true` as a **pre-call** gate and still
  throws if the engine reports pending after that gate passes.
- `langchain-algenta` can pause **mid-call**, genuinely resumably, via
  `langgraph.types.interrupt()` -- with a hand-built single-retry-after-resume `retry` callback.
- `maf-algenta` has a real **pre-call** gate (`approval_mode`) and a hard, fail-closed **abort**
  when the engine's own state is still pending after that gate.
- `haystack-algenta` has no mid-call pause at all -- its `after_tool` hook is a one-shot,
  fail-closed abort with no resumption.
- `llamaindex-algenta` (this package) is the only one of the six with a real **mid-call** pause
  (`Context.wait_for_event()`, like LangGraph's `interrupt()`) that **also gets its retry for
  free** from the runtime's own replay-the-step semantics, instead of needing a hand-built retry
  path.

### Exception propagation once it leaves this package

`FunctionTool.acall()` has no `try`/`except` anywhere -- `AlgentaToolDenied` /
`AlgentaToolExecutionFailed` / `AlgentaApprovalStillPending` propagate out of it completely
unmodified. What happens next depends entirely on what calls the tool:

- A bare `await tool.acall(...)` lets the exception through as-is.
- `llama_index.core.tools.calling.acall_tool`, or a real `FunctionAgent`/`AgentWorkflow` run
  (`BaseWorkflowAgent._call_tool`), catches *any* exception and turns it into an ordinary
  `ToolOutput(is_error=True, exception=e)` -- verified live with a deliberately-blowing-up test
  tool (`tests/stub_server.py`'s `blow_up`) -- **except** the internal `WaitingForEvent` control-flow
  exception `wait_for_event()` itself raises, which is the one exception type
  `BaseWorkflowAgent._call_tool` explicitly re-raises rather than swallowing (mirroring MAF's
  `MiddlewareFailure` carve-out and Haystack's `after_tool`-hook precedent for their own runtimes).

A caller that wants `AlgentaToolDenied`/`AlgentaToolExecutionFailed`/`AlgentaApprovalStillPending`
to actually stop a `FunctionAgent.run()` must inspect the `ToolCallResult`/`ToolOutput` it produces
(`tool_output.is_error`, `tool_output.exception`), not wrap `agent.run()` in a `try`/`except`.
Documented honestly here rather than papering over it -- see `llamaindex_algenta/exceptions.py`'s
module docstring for the full accounting.

## Receipt citations

LlamaIndex's real citation primitives -- `CitableBlock`/`CitationBlock` (chat-message content
blocks for provider-native citation features) and classic-RAG `Response.source_nodes` -- are both
about grounding *generated text* or a *synthesized answer* in retrieved source content. Neither has
anything to do with a tool call's execution metadata: `FunctionTool._parse_tool_output` only ever
emits a `CitableBlock`/`CitationBlock` if a tool's raw output *already is* one, and an MCP
`CallToolResult` never is. There is no real API seam in `llama-index-core`/`llama-index-tools-mcp`
that reads `request_id`/`trace_id`/`plan_hash`/`policy_snapshot_hash` for any citation purpose --
so this package does not build one; inventing a "receipt citation" primitive here would be exactly
the kind of unverified, hoped-for feature this whole track's research is meant to rule out.

What this package *does* give you is the receipt itself, fully typed, on `ToolOutput.raw_output`
for every governed call. If you want those fields to show up in a citation-aware LlamaIndex chat
UI, build your own `CitableBlock` from them -- that's a few lines in your own code, not package
machinery:

```python
from llama_index.core.base.llms.types import CitableBlock, TextBlock

def receipt_as_citable_block(receipt, tool_name: str) -> CitableBlock:
    return CitableBlock(
        title=f"Algenta: {tool_name}",
        source=f"plan_hash={receipt.plan_hash} request_id={receipt.request_id}",
        content=[TextBlock(text=str(receipt.result))],
    )
```

## The `mcp<2.0.0` pin -- a real, currently-live upstream bug

`pip install llama-index-core llama-index-tools-mcp` today resolves `mcp==2.0.0` (the only version
satisfying `llama-index-tools-mcp` 0.5.0's own `mcp<3,>=2.0.0` constraint). Every `BasicMCPClient`
HTTP call against a real server then throws:

```
ValueError: not enough values to unpack (expected 3, got 2)
```

Verified live, read from real source: `llama_index/tools/mcp/client.py`'s
`BasicMCPClient._run_session` still unpacks `mcp.client.streamable_http.streamable_http_client(...)`'s
yield as a 3-tuple `(read, write, _)`, but installed `mcp==2.0.0`'s `streamable_http_client` now
yields a 2-tuple. Pinning `mcp<2.0.0` in this package's own `pyproject.toml` makes the dependency
resolver pick `llama-index-tools-mcp` 0.4.8 instead (whose own `Requires-Dist` is `mcp<2,>=1.24.0`),
whose HTTP transport is verified working end-to-end against a real server -- this package's own
`uv.lock` in this monorepo resolves exactly that combination
(`llama-index-core==0.14.24`, `llama-index-tools-mcp==0.4.8`, `mcp==1.29.0`).

A stdio transport also works around this on `mcp==2.0.0`/`llama-index-tools-mcp` 0.5.0 (its code
path splat-unpacks instead of a fixed-arity unpack), but self-hosted Algenta engines are reached
over HTTP in every sibling package's design, so pinning `mcp<2.0.0` is the realistic fix here, not
"use stdio instead." If upstream fixes `client.py`, or the day it makes sense to move off `mcp<2`,
loosen this pin deliberately -- don't just widen it blindly, since the whole point of this section
is that the wider range is silently broken today.

## Why `llama-index-tools-mcp`, not the `llama-index` metapackage

Verified directly from `llama-index-tools-mcp`'s installed `.dist-info/METADATA`: its own
`Requires-Dist` is only `llama-index-core`, `mcp`, and `pydantic`. `pip install llama-index-core
llama-index-tools-mcp` never pulls in the full `llama-index` metapackage or any model-provider SDK
it depends on -- the same "only the surface this package actually needs" reasoning
`pydantic-ai-slim[mcp]` (D1) and `agent-framework-core` (D5) already document for their own
frameworks.

## Why no `algenta-sdk` dependency

Every package in this repository may depend on at most one Algenta-owned thing, the published
`algenta-sdk` client -- but only if it's genuinely used. This package never imports it:
`create_algenta_tools` talks to the caller's self-hosted Algenta MCP endpoint directly via
`llama_index.tools.mcp.BasicMCPClient`, the same reason `maf-algenta` (D5) and
`typescript/algenta-tools` (D2) declare no `algenta-sdk` dependency either. Declaring it anyway,
without importing it anywhere in this package's own source, would repeat exactly the
leftover-placeholder-dependency pattern an adversarial review is on record catching elsewhere in
this repository's history.

## Testing this package's own test suite (not your agent)

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest llamaindex-algenta -v
```

This repository's CI runs exactly that isolated command per package (never `pytest .` across
multiple packages at once) -- two packages sharing a `tests/__init__.py` module name would
otherwise collide in one shared invocation. `tests/stub_server.py` runs a real `fastmcp.FastMCP`
server over a real local HTTP socket, so a wire-shape regression (e.g. the receipt envelope not
surviving `structuredContent` round-tripping) would actually be caught, not hidden behind a mock.
`tests/test_approval_hitl.py` exercises the real pause/resume round trip through a real
`FunctionAgent.run()`, and `tests/test_contract_parity.py` loads
`contracts/integration-tool-contract.json` from disk rather than hardcoding profile membership.
