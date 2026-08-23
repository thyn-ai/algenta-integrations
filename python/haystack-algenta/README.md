# haystack-algenta

Haystack tool integration for [Algenta](https://algenta.ai): `create_algenta_tools`, a
governed-execution-aware `haystack.tools.toolset.Toolset` wrapping your own self-hosted Algenta
Engine's MCP tool surface via Haystack's own real
[`MCPToolset`](https://docs.haystack.deepset.ai/docs/mcptoolset) (a separate PyPI package,
`mcp-haystack` -- see [Why `mcp-haystack`, not just `haystack-ai`](#why-mcp-haystack-not-just-haystack-ai)),
plus `build_algenta_governance_hooks`, an `Agent` `before_tool`/`after_tool` hook pair mapping the
governed-execution receipt contract onto Haystack's own real `ConfirmationHook` human-in-the-loop
primitive and `after_tool` hook seam.

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
  This is Haystack's own native `MCPToolset(tool_names=...)` mechanism -- no wrapper needed for
  this half at all.
- **Two-layer never-model-facing scrubbing** -- `force`/`override_safety` are stripped from every
  returned tool's advertised schema *and* from the arguments dict actually forwarded to the real
  call.
- **Typed governed-execution receipts** -- parseable via `extract_receipt_from_tool_result`.
- **An honest approval mapping** -- see [Approval mapping](#approval-mapping) below for the full
  accounting of what Haystack's real primitives do and don't guarantee, verified live against
  installed `haystack-ai` 3.0.0 / `mcp-haystack` 1.4.1, not assumed from documentation.

## Install

```bash
pip install haystack-algenta
```

This package depends on **`haystack-ai`** and **`mcp-haystack`** (both real, non-optional runtime
dependencies) and deliberately **not** on `algenta-sdk` -- see
[Why no `algenta-sdk` dependency](#why-no-algenta-sdk-dependency).

## Self-hosted-first

`create_algenta_tools` talks to **your own self-hosted Algenta Engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the function,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```python
from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack_algenta import create_algenta_tools

toolset = create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")
agent = Agent(chat_generator=OpenAIChatGenerator(), tools=toolset)
result = agent.run(messages=[...])
toolset.close()  # tears down the MCP connection this call built
```

`profile="observe"` is also the default if you omit it -- the agent can call `get_contract` /
`query_data` / `simulate` / `recommend`, and nothing that writes, plans, or executes anything.

**Unlike this repository's other four Python siblings, everything here is synchronous.**
`MCPToolset`'s real, public API (`warm_up()`, `Tool.invoke()`, `Agent.run()`) is itself
synchronous -- it bridges the real async MCP client through its own internal `AsyncExecutor` --
so `create_algenta_tools` is a plain function, not an `async def`, and there is no `pytest-asyncio`
anywhere in this package's own test suite. A genuine framework-level difference from
`pydantic-ai-algenta`/`langchain-algenta`/`litellm-algenta`/`maf-algenta`, not an oversight.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution, approval-gated -- see below. |
| `full` | everything the connected engine advertises | Opt-in only; admin/ops tooling. |

```python
toolset = create_algenta_tools(base_url="...", profile="execute")
```

An `observe`-profile call genuinely does not return `execute_decision` (or anything
`govern`/`execute`-tier) -- filtered at `MCPToolset` construction time via `tool_names=...`, so
this package never even sees the excluded tools in the first place. `"full"` deliberately omits
`tool_names` (matching the contract's own `tools: "*"` sentinel), which puts it well past the
20-30-tool threshold deepset's own `MCPToolset` docs warn can overwhelm an LLM's tool-resolution
logic (the real contract's `full` profile is ~117 tools) -- `haystack-ai`'s own `SearchableToolset`
(semantic tool search instead of dumping every schema into context) is the documented mitigation
for that case, worth pairing with `profile="full"` in your own agent if you use it; this package
doesn't force that choice on you.

## Approval mapping

Verified live against installed `haystack-ai` 3.0.0 (a real `mcp.server.fastmcp.FastMCP` stub
server, real HTTP wire, real `Agent` run loop -- see `tests/test_toolset_scenarios.py`), not
assumed from documentation. Haystack has no single seam that can both gate a pending call *and*
see the receipt that call eventually produces, the way `maf_algenta`'s one wrapped `FunctionTool`
does. It has two separate, real primitives this package wires together instead:

**1. `haystack.hooks.human_in_the_loop.ConfirmationHook` -- a genuine pre-call gate.** A real
`before_tool` `Agent` hook. Proven live: with `AlwaysAskPolicy()` and a UI that rejects, the real
MCP server is never contacted at all (no request logged) -- the same shape as MAF's
`approval_mode="always_require"` or the TypeScript sibling's `needsApproval`.
`default_confirmation_hook`/`build_algenta_governance_hooks(confirmation_ui=...)` are thin,
opinionated factories over it, defaulting to gating `execute_decision` only with
`AlwaysAskPolicy()` (opting into the gate at all is itself the opt-in -- this contract's `execute`
profile must never be reachable by default).

**2. `GovernedReceiptHook` -- this package's own `after_tool` hook.** Parses the governed-execution
receipt out of the tool-result message(s) `Agent._run_step` just wrote into
`state.data["messages"]`, and raises `AlgentaToolDenied` / `AlgentaApprovalStillPending` /
`AlgentaToolExecutionFailed` for anything that isn't a clean success. Proven live that this
exception propagates out of `agent.run()` **completely unmodified** -- because
`Agent._run_step`'s `_run_hooks(self.hooks, AFTER_TOOL, state)` call has no surrounding
`try`/`except` anywhere in `agent.py` (grep-verified against the installed package). This is
genuinely the *only* seam in Haystack's `Agent` loop that behaves this way.

**Why not map inside the wrapped `Tool.function` the way every other sibling package does?**
Verified live, and it is the strictest exception behavior of all five sibling frameworks:
`Tool.invoke()`/`invoke_async()` has a **bare `except Exception`** that unconditionally catches
and rewraps *anything* into `haystack.tools.errors.ToolInvocationError` (losing the original
exception's type, though not its message -- recoverable via `.__cause__`), with no escape hatch at
that layer at all (unlike pydantic-ai's dedicated `ApprovalRequired`/`ToolDenied`/`ToolFailed`, or
MAF's `MiddlewareFailure`, which is explicitly excluded from wrapping). And it gets swallowed, not
just wrapped: a real `Agent`'s documented default is `raise_on_tool_invocation_failure=False`, and
with that default the wrapped exception is never even re-raised -- it's converted into an ordinary
error-flagged `ChatMessage` fed back into the conversation, and `agent.run()` returns *normally*.
Raising a governance exception from inside `haystack_algenta.toolset`'s wrapped calls would
therefore be exactly the wrong seam for the common, real-`Agent` case -- proven, not assumed
(`tests/test_toolset_scenarios.py::test_a_blows_up_tool_error_is_wrapped_and_swallowed_by_default`
reproduces this with a real server-side exception, unrelated to governance, to isolate the claim).

**Why both hooks are needed, and why they don't collapse into one concern:** confirming the *call*
(via `ConfirmationHook`) is a decision about whether the model may attempt `execute_decision` at
all. It says nothing about whether the connected engine's own out-of-band policy approval for that
call's `plan_hash` has actually been recorded. Verified live: after a human confirms the call
through Haystack's gate, the engine's own receipt can still legitimately come back
`approval_state="pending"` if nobody separately called the engine's real approval endpoint for
that `plan_hash` -- Haystack's pre-call gate has no idea this happened; it's an ordinary successful
tool call as far as the `Agent` is concerned until `GovernedReceiptHook` looks at the receipt.
That is exactly what `AlgentaApprovalStillPending` represents -- and there is no Haystack-native
resumable pause to fall back to at that point, unlike `langchain-algenta`'s
`langgraph.types.interrupt()`. This is a one-shot, fail-closed abort: record the real approval
against `receipt.plan_hash` out of band, then retry with a fresh `agent.run()`.

A call the engine denies outright (a named policy-gate `code` such as `plan_hash_mismatch`,
`stale_plan`, `plan_not_approved`, `idempotency_key_conflict`, or `approval_state in ("rejected",
"expired")`) raises `AlgentaToolDenied`; anything else that isn't a recognized success raises
`AlgentaToolExecutionFailed`.

### Where this sits relative to the other four siblings

- `pydantic-ai-algenta` raises `ApprovalRequired` -- a **post-hoc** reaction after the real MCP
  call already happened, because pydantic-ai has no pre-call approval gate at all.
- `algenta-tools` (Vercel AI SDK) sets `needsApproval: true` as a **pre-call** gate and still
  throws if the engine reports pending after that gate passes, because AI SDK has no mid-call
  pause primitive to fall back on.
- `langchain-algenta` can pause **mid-call**, genuinely resumably, via
  `langgraph.types.interrupt()` -- when a checkpointer is present.
- `maf-algenta` has a real **pre-call** gate (`approval_mode`) and a hard, fail-closed **abort**
  when the engine's own state is still pending after that gate, raised from *inside* the same
  wrapped tool call (because `agent_framework.MiddlewareFailure` is explicitly excluded from that
  framework's own exception-wrapping).
- `haystack-algenta` (this package) is the only one of the five where the pre-call gate
  (`ConfirmationHook`) and the fail-closed abort (`GovernedReceiptHook`) **cannot live in the same
  place** -- Haystack's tool-invocation layer wraps and swallows too aggressively for that, so the
  abort has to live one level up, in a dedicated `after_tool` hook, the one seam proven to survive
  intact.

### A `Pipeline`, or any direct `Tool.invoke()` caller, gets neither hook

Hooks are an `Agent`-loop concept. `build_algenta_governance_hooks` only helps agents built with
`haystack.components.agents.Agent`. For a `Pipeline`, or any caller invoking `Tool.invoke()`/
`invoke_async()` directly, `haystack_algenta.extract_receipt_from_tool_result` is the same parsing
`GovernedReceiptHook` uses, exposed directly so you can call it yourself on whatever `invoke()`
returned and decide what to do:

```python
from haystack_algenta import extract_receipt_from_tool_result

raw_result = execute_decision_tool.invoke(plan_hash="...", idempotency_key="...")
receipt = extract_receipt_from_tool_result(raw_result)
if receipt is not None and not receipt.is_success():
    ...  # your own handling -- this package makes no pause/resume claim for this path
```

This is an honest, undisguised capability, not a fabricated approval-pause mechanism -- the same
stance `litellm_algenta` documents for its own no-pause gateway path.

## Why the double-JSON unwrap

Verified live: `Tool.invoke()` for an `MCPToolset`-built tool returns the raw MCP
`CallToolResult`, JSON-*serialized*, with the actual tool payload nested as a JSON *string* inside
a text content block inside that JSON:

```json
{"meta": null, "content": [{"type": "text", "text": "{\n  \"status\": \"ok\", ... \"approval_state\": \"pending\", ...}"}], "structuredContent": null, "isError": false}
```

Neither `Tool` nor `MCPToolset` ever unwraps this -- there is zero receipt concept anywhere in
Haystack's own tool-calling layer, so a governed call and `get_contract`'s plain discovery blob
are handled completely identically (both are just opaque strings) until something -- this
package's `unwrap_mcp_tool_result` -- peels both string layers back into a real Python value.

## Why `mcp-haystack`, not just `haystack-ai`

`MCPToolset` is **not part of `haystack-ai`** -- confirmed by grepping an installed `haystack-ai`
3.0.0 tree for any `mcp` module: none exists there. It ships in a separate PyPI package,
**`mcp-haystack`**, under the `haystack_integrations.tools.mcp` import namespace. `haystack-ai`
core only has the generic `Tool`/`Toolset`/`ComponentTool`/`Agent`/hooks primitives this package
also uses. Any consumer of this package gets both transitively (they're both real, non-optional
dependencies here), but it's worth knowing they're two separate packages if you're pinning
versions yourself.

## Why no `algenta-sdk` dependency

Every package in this repository may depend on at most one Algenta-owned thing, the published
`algenta-sdk` client -- but only if it's genuinely used. This package never imports it:
`create_algenta_tools` talks to the caller's self-hosted Algenta MCP endpoint directly via
`haystack_integrations.tools.mcp.MCPToolset`, the same reason `maf-algenta` (D5) and
`typescript/algenta-tools` (D2) declare no `algenta-sdk` dependency either. Declaring it anyway,
without importing it anywhere in this package's own source, would repeat exactly the
leftover-placeholder-dependency pattern an adversarial review is on record catching elsewhere in
this repository's D2/D4 history.

## Not a `WrapperToolset`/interceptor

Every real, in-process sibling in this repository (`pydantic-ai-algenta`, `langchain-algenta`,
`maf-algenta`) builds one wrapper object that filters, scrubs, *and* maps governance in a single
seam. This package deliberately does not: `haystack_algenta.toolset` only does profile filtering
(native, via `MCPToolset(tool_names=...)`) and the two-layer scrub; `haystack_algenta.hooks` is a
separate, `Agent`-level hook pair. Haystack genuinely has no single interception point on
`MCPToolset`/`Tool` to build a combined wrapper on top of -- `Toolset`'s entire public surface is
`add`/`from_dict`/`get_selectable_tools`/`spawn`/`to_dict`/`warm_up`, and `MCPToolset` builds each
`Tool`'s call as a closure with no `tool_interceptors=`-style hook anywhere (grep-verified against
the installed package). Rebuilding each `Tool` via `dataclasses.replace` (what this package does
for the scrub) is the same escape-hatch pattern `langchain_algenta._wrap_plain_tool` and
`maf_algenta._wrap_mcp_function` already use for their own non-native-seam cases; the receipt
mapping just has to live one layer higher here than it does for them.

## Testing this package's own test suite (not your agent)

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest haystack-algenta -v
```

This repository's CI runs exactly that isolated command per package (never `pytest .` across
multiple packages at once) -- two packages sharing a `tests/__init__.py` module name would
otherwise collide in one shared invocation. `tests/stub_server.py` runs a real
`mcp.server.fastmcp.FastMCP` server over a real local HTTP socket, on its own background thread
(not an asyncio task on the same loop the synchronous `MCPToolset`/`Agent` calls block on -- see
that file's module docstring), so a wire-shape regression would actually be caught, not hidden
behind a mock.
