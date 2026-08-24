# llamaindex-algenta

LlamaIndex tool integration for [Algenta](https://algenta.ai): `create_algenta_tools`, an
outcome-aware `list[llama_index.core.tools.FunctionTool]` wrapping your own self-hosted Algenta
Engine's MCP tool surface via `llama-index-tools-mcp`'s own real
[`BasicMCPClient`](https://docs.llamaindex.ai/en/stable/api_reference/tools/mcp/) (a separate PyPI
package -- see [Why `llama-index-tools-mcp`, not the `llama-index`
metapackage](#why-llama-index-tools-mcp-not-the-llama-index-metapackage)), plus a real,
synchronous receipt/denial mapping for `execute_decision` -- the one safety-critical tool in the
real contract.

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **Two-layer never-model-facing scrubbing** -- `force`/`override_safety` are stripped from every
  returned tool's advertised pydantic schema *and* from the arguments dict actually forwarded to
  the real `call_tool(...)`.
- **A typed execution receipt, not a stringified blob** -- a successful `execute_decision` call's
  `ToolOutput.raw_output` is the parsed `ExecutionReceipt` object itself.
- **An honest execution-outcome mapping** -- see [Execution outcome
  mapping](#execution-outcome-mapping) below for exactly what `execute_decision` does on success
  and on each of its three real named denial gates -- there is no asynchronous "pending approval"
  state anywhere in the real tool, and this package does not pretend there is one.

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
| `execute` | + `execute_decision` | Real-world execution, gated -- see below. |
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

-- kwargs are forwarded to the real MCP call completely unscrubbed, and the raw
`mcp.types.CallToolResult` comes straight back with no outcome mapping applied at all.
`create_algenta_tools` needs two things that closure doesn't give it, for every allowed tool call:
the two-layer `force`/`override_safety` scrub, and (for `execute_decision` specifically) turning a
real success/denial into a typed `ExecutionReceipt` or a raised `AlgentaToolDenied`. This module
therefore builds its own `async def wrapper(**kwargs)` per allowed tool and hands that to
`FunctionTool.from_defaults(...)` directly -- reusing only `McpToolSpec`'s public
JSON-Schema-to-pydantic-model helpers (`create_model_from_json_schema`, `remove_model_fields`),
never its tool-call closures or `fetch_tools`/`to_tool_list_async`.

**A footgun this design used to carry, and doesn't anymore:** an earlier version of `wrapper` took
a `ctx: Context` parameter, so it could call `Context.wait_for_event()` -- llama-index-workflows'
real human-in-the-loop pause primitive -- for a fictional, asynchronous `approval_state ==
"pending"` receipt state that does not exist on the real `execute_decision` tool (see [Execution
outcome mapping](#execution-outcome-mapping) below). Because `FunctionTool.__init__` detects
whether a wrapped function needs workflow context by inspecting the *raw*, unresolved annotation
on a `Context`-typed parameter, that `ctx: Context` parameter was the one real reason
`llamaindex_algenta/toolset.py` used to omit `from __future__ import annotations` (every sibling
module in this package uses it) -- with the future import enabled, the annotation would have
silently become the *string* `"Context"` at runtime, `requires_context` would have silently come
back `False`, and every real `FunctionAgent` call would have failed with a plain `TypeError:
wrapper() missing 1 required positional argument: 'ctx'`. Now that `execute_decision`'s real
behavior is fully synchronous (see below), there is nothing left to pause on, `wrapper` no longer
takes a `ctx` parameter at all, and with no `Context`-annotated parameter anywhere in this module
the footgun no longer applies -- `toolset.py` uses `from __future__ import annotations` like every
sibling module now.

## Execution outcome mapping

Only `execute_decision` gets any outcome mapping at all -- it is the one tool in the real contract
marked `safety_critical`. `plan_decision`, `log_decision`, `query_data`, `simulate`, `recommend`,
and `get_contract` are none of them gated; each returns its own plain result, passed straight
through to the model unchanged.

`execute_decision(decision_id, webhook_url, timeout_seconds=None, force=False,
override_safety=False, metadata=None)` has exactly two real outcomes, both synchronous, in the
same call -- there is no asynchronous "pending, come back later" state anywhere on this tool:

- **Success**: a real `ExecutionReceipt` --
  `{decision_id, webhook_url, execution_status: "delivered" | "failed", response_code,
  executed_at, policy_snapshot_id, schema_snapshot_id, manifest_version, payload_summary,
  safety_overridden}`. `create_algenta_tools` returns this parsed, typed object as
  `ToolOutput.raw_output` -- not a stringified blob.
- **Blocked**: a real, synchronous HTTP 409 whose body is `{"error": {"code":
  "execution_blocked_<gate>", "gate": "<gate>", "message": "...", "override_hint": "..."}}`,
  where `<gate>` is exactly one of three named values: `"idempotency"` (already delivered;
  `force=true` overrides for one re-execution), `"confidence"` (below `policy.min_confidence`), or
  `"risk_floor"` (`risk_p5` below `-policy.risk_floor`) -- the latter two bypassable only via
  `override_safety=true`. `create_algenta_tools` raises `AlgentaToolDenied` for this, with
  `error.gate`/`error.code`/`error.override_hint` carried on the exception verbatim.

Anything else that isn't a recognized success or a recognized named-gate denial -- including the
MCP protocol-level `isError=True` case (a server-side tool crash the MCP SDK already turned into
ordinary, non-raising response data) -- raises `AlgentaToolExecutionFailed`. `force` and
`override_safety` are the two fields that resolve a denial, and neither is ever model-facing (see
[Tool profiles](#tool-profiles) above) -- an agent that receives `AlgentaToolDenied` cannot itself
retry past it; only a human operator calling outside the model-facing tool surface can.

A genuinely separate, real, `plan_hash`+nonce human-approval system does exist in the real engine
(decision-cases/analysis-runs/decision-plans HTTP routes) -- but it is intentionally not exposed as
an MCP/LLM tool at all, so no integration package (this one included) can reach it, and this
package does not pretend otherwise.

### Exception propagation once it leaves this package

`FunctionTool.acall()` has no `try`/`except` anywhere -- `AlgentaToolDenied` /
`AlgentaToolExecutionFailed` propagate out of it completely unmodified. What happens next depends
entirely on what calls the tool:

- A bare `await tool.acall(...)` lets the exception through as-is.
- `llama_index.core.tools.calling.acall_tool`, or a real `FunctionAgent`/`AgentWorkflow` run
  (`BaseWorkflowAgent._call_tool`), catches *any* exception and turns it into an ordinary
  `ToolOutput(is_error=True, exception=e)` instead -- verified live with a deliberately-blowing-up
  test tool (`tests/stub_server.py`'s `blow_up`) and with a real named-gate denial
  (`tests/test_toolset_scenarios.py`). Since `execute_decision`'s real outcome is always decided
  within the one tool-call step -- never a pause -- this is the only path a real `FunctionAgent`
  run through this tool ever takes: it finishes on the very same turn regardless of whether the
  call succeeded or was denied.

A caller that wants `AlgentaToolDenied`/`AlgentaToolExecutionFailed` to actually stop a
`FunctionAgent.run()` must inspect the `ToolCallResult`/`ToolOutput` it produces
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
that reads any of `ExecutionReceipt`'s fields for any citation purpose -- so this package does not
build one; inventing a "receipt citation" primitive here would be exactly the kind of unverified,
hoped-for feature this whole track's research is meant to rule out.

What this package *does* give you is the receipt itself, fully typed, on `ToolOutput.raw_output`
for a successful `execute_decision` call. If you want those fields to show up in a citation-aware
LlamaIndex chat UI, build your own `CitableBlock` from them -- that's a few lines in your own code,
not package machinery:

```python
from llama_index.core.base.llms.types import CitableBlock, TextBlock

def receipt_as_citable_block(receipt, tool_name: str) -> CitableBlock:
    return CitableBlock(
        title=f"Algenta: {tool_name}",
        source=f"decision_id={receipt.decision_id} response_code={receipt.response_code}",
        content=[TextBlock(text=str(receipt.payload_summary))],
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
`tests/test_toolset_scenarios.py` exercises all three real named `execute_decision` gates plus the
success path, both via a direct `tool.acall(...)` and through a real `FunctionAgent.run()` (proving
each run finishes on its very first turn, with no `InputRequiredEvent`/pause of any kind ever
emitted -- there is nothing asynchronous to pause on), and `tests/test_contract_parity.py` loads
`contracts/integration-tool-contract.json` from disk rather than hardcoding profile membership.
