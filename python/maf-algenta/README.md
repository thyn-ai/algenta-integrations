# maf-algenta

Microsoft Agent Framework (MAF) tool integration for [Algenta](https://algenta.ai):
`create_algenta_tools`, an async context manager that builds a governed-execution-aware list of
[`agent_framework.FunctionTool`](https://github.com/microsoft/agent-framework)s from your own
self-hosted Algenta Engine's MCP tool surface, and layers on:

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
- **Two-layer never-model-facing scrubbing** -- `force`/`override_safety` are stripped from
  every returned tool's advertised schema *and* from the arguments dict actually forwarded to
  the real call, and here (unlike this repository's other Python siblings) both layers are load-
  bearing, not just belt-and-suspenders -- see [Why two layers, verified](#why-two-layers-verified).
- **Typed governed-execution receipts** -- every tool call's result is parseable into a
  [`GovernedExecutionReceipt`][receipt] via `parse_receipt`.
- **An honest approval mapping** built on MAF's own two real primitives --
  `approval_mode="always_require"` (a genuine pre-call human-in-the-loop gate) and
  `agent_framework.MiddlewareFailure` (MAF's one fail-closed abort signal) -- with a full
  accounting below of what each one does and doesn't guarantee.

[receipt]: ./maf_algenta/receipts.py

**This package is about Microsoft Agent Framework only.** Microsoft Foundry (the hosted Azure
platform) is a separate, documentation-only deliverable under [`foundry/`](./foundry/README.md)
-- read that file's opening paragraph before assuming anything here was verified against a live
Foundry project, because it wasn't.

## Install

```bash
pip install maf-algenta
```

This package depends on **`agent-framework-core`** (not the `agent-framework` umbrella package)
and `mcp`, both real, non-optional runtime dependencies -- and deliberately **not** on
`algenta-sdk`. See [Why `agent-framework-core`, not `agent-framework`](#why-agent-framework-core-not-agent-framework)
and [Why no `algenta-sdk` dependency](#why-no-algenta-sdk-dependency) below for both, rather than
assuming either is an oversight.

## Self-hosted-first

`create_algenta_tools` talks to **your own self-hosted Algenta Engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the function,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```python
from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient  # or any other real chat client
from maf_algenta import create_algenta_tools

# create_algenta_tools is an async context manager -- it owns the MCP connection for the
# lifetime of the `with` block (see "Why an async context manager" below).
async with create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe") as tools:
    agent = Agent(OpenAIChatClient(), tools=tools)
    result = await agent.run("What's the expected value of scenario X?")
    print(result.text)
```

`profile="observe"` is also the default if you omit it -- the agent can call `get_contract` /
`query_data` / `simulate` / `recommend`, and nothing that writes, plans, or executes anything.
See [Tool profiles](#tool-profiles) to opt into more.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution, approval-gated -- see below. |
| `full` | everything the connected engine advertises | Opt-in only; admin/ops tooling. |

```python
async with create_algenta_tools(base_url="...", profile="execute") as tools:
    ...
```

An `observe`-profile call genuinely does not return `execute_decision` (or anything
`govern`/`execute`-tier) -- filtered twice: once at MCP connection time via
`MCPStreamableHTTPTool(allowed_tools=...)`, so this package never even sees the excluded
functions in the first place, and again as a defense-in-depth pass over whatever `.functions`
comes back (`resolve_profile_tool_names`), which is the only filter that runs at all for the
`mcp_tool=` escape hatch (see [The `mcp_tool=` escape hatch](#the-mcp_tool-escape-hatch)).

## Why two layers, verified

`force` / `override_safety` are stripped from every returned tool's advertised `input_model`
schema *and* scrubbed from the arguments dict actually forwarded to the real underlying call.
Every sibling package in this repository does this defensively, "just in case" -- for
`maf-algenta`, verified directly against the installed `agent-framework-core` 1.15.0 source, the
call-time layer is not optional:

`agent_framework._tools._validate_arguments_against_schema` -- the function MAF actually runs to
check a model-supplied argument dict against a tool's schema before invoking it -- only rejects
an *unexpected* property when the schema explicitly sets `"additionalProperties": false`. Real
MCP-derived tool schemas do not set that. This was proven live, not assumed: a scripted model
call supplying `{"plan_hash": ..., "force": true}` against a schema with `force` already removed
sails straight through validation, and the real underlying `execute_decision` call would have
received `force=True` if the call-time scrub weren't also there. `tests/test_never_model_facing.py`
and `tests/test_toolset_scenarios.py::test_smuggled_force_never_reaches_the_real_server_over_the_real_wire`
both assert on the real received arguments (via the stub server's own `forced` echo field), not
just on the advertised schema.

## Approval mapping

`execute_decision` is real-world execution and is approval-gated, using the two real MAF
primitives this package's research pass found -- verified live against the installed
`agent-framework-core` 1.15.0, not assumed from documentation:

**1. `approval_mode="always_require"` -- a genuine pre-call, human-in-the-loop gate.** Every
`FunctionTool` `create_algenta_tools` returns for `execute_decision` carries this. When the model
requests the call, MAF's own function-invocation loop pauses *before* calling anything: the run
returns with `Content(type="function_approval_request")` instead of a result, and the real
underlying tool has genuinely not been called yet (verified: `tests/test_toolset_scenarios.py::test_execute_decision_pauses_for_approval_via_real_agent_run`
asserts the pause and that no call happened). Resuming requires appending a
`Content.from_function_approval_response(..., approved=True)` and calling `agent.run()` again on
the next turn -- this is analogous in spirit to `algenta-tools` (Vercel AI SDK)'s `needsApproval`
(a real pre-call gate), but framework-native rather than MCP-adapter-specific, and it resumes on
the next model turn rather than mid-call.

**2. `agent_framework.MiddlewareFailure` -- MAF's one fail-closed abort signal.** Quoted directly
from the installed package's own docstring: "Ordinary exceptions raised by function middleware
(or by the tool it wraps) are converted into tool-error results ... `MiddlewareFailure` is the
loop's explicit fail-closed escape: it is never converted into a tool result, ... and the
exception propagates to the caller of `Agent.run`." Verified live, including the specific case
this package relies on -- raising it directly from a tool's own body (not from a
`FunctionMiddleware`), with **zero middleware registered on the agent at all**, still propagates
unmodified out of `agent.run()`.

**Why both are needed, and why they don't collapse into one concern:** approving the *call* (via
`approval_mode`) is a decision about whether the model may attempt `execute_decision` at all. It
says nothing about whether the connected engine's own out-of-band policy approval for that call's
`plan_hash` has actually been recorded. Verified live: after a human approves the call through
MAF's gate, the engine's own receipt can still legitimately come back `approval_state="pending"`
if nobody separately called the engine's real approval endpoint for that `plan_hash`. That is
exactly what `maf_algenta.exceptions.AlgentaApprovalStillPending` (an `AlgentaGovernedCallFailure`,
which is an `agent_framework.MiddlewareFailure`) represents -- and, per the finding above, there
is no MAF-native resumable pause to fall back to at that point, unlike `langchain-algenta`'s
`langgraph.types.interrupt()`. This is a one-shot, fail-closed abort: record the real approval
against `receipt.plan_hash` out of band, then retry the call from a fresh model turn.

A call the engine denies outright (a named policy-gate `code` such as `plan_hash_mismatch`,
`stale_plan`, `plan_not_approved`, `idempotency_key_conflict`, or `approval_state in ("rejected",
"expired")`) raises `AlgentaToolDenied`; anything else that isn't a recognized success raises
`AlgentaToolExecutionFailed`. All three inherit `AlgentaGovernedCallFailure`, which inherits
`agent_framework.MiddlewareFailure` -- so `isinstance(exc, agent_framework.MiddlewareFailure)` is
always true for anything this package raises, and nothing here can silently be swallowed into a
tool-error result the model then sees and might paper over.

### Where this sits relative to the other three siblings

- `pydantic-ai-algenta` raises `ApprovalRequired` -- a **post-hoc** reaction after the real MCP
  call already happened, because pydantic-ai has no pre-call approval gate at all.
- `algenta-tools` (Vercel AI SDK) sets `needsApproval: true` as a **pre-call** gate and still
  throws if the engine reports pending after that gate passes, because AI SDK has no mid-call
  pause primitive to fall back on.
- `langchain-algenta` can pause **mid-call**, genuinely resumably, via
  `langgraph.types.interrupt()` -- when a checkpointer is present.
- `maf-algenta` (this package) is closest in shape to `algenta-tools`: a real **pre-call** gate
  (`approval_mode`), and a hard, fail-closed **abort** rather than a pause when the engine's own
  state is still pending after that gate -- because, like AI SDK, MAF has no mid-call resumable
  primitive either. The one thing this package can do that none of the three others can: raise
  its abort signal directly from inside the wrapped tool's own body, with no middleware
  registration required on the caller's `Agent` at all, since `MiddlewareFailure` propagates
  unmodified from either location.

## The `mcp_tool=` escape hatch

`create_algenta_tools(mcp_tool=..., profile=...)` accepts anything exposing a `.functions: list[FunctionTool]`
-- typically an already-connected `agent_framework.MCPTool` you built and are managing the
lifecycle of yourself, or (as `tests/test_profile_filtering.py` and `tests/test_never_model_facing.py`
do) a fake in-memory registry with no MCP connection at all, for testing pure list-filtering and
schema-scrubbing logic without a network round trip. **You own that value's connection lifecycle**
-- this function only ever reads `.functions` off it and never calls `.connect()`/`.close()`.
Mutually exclusive with `base_url` and any MCP-connection keyword argument.

## Why an async context manager

`agent_framework.MCPStreamableHTTPTool` is itself used as `async with MCPStreamableHTTPTool(...) as
mcp_tool:` in MAF's own examples and documentation -- one instance *is* a live, connected client
session; there is no separate "client" object this package could build once and reuse across
calls the way `langchain-mcp-adapters`' `MultiServerMCPClient` works. `create_algenta_tools`
mirrors that lifecycle exactly rather than inventing a different shape: it owns a fresh connection
for the duration of the `async with` block (unless you supply `mcp_tool=`, in which case you own
it) and tears it down on exit.

## Why `agent-framework-core`, not `agent-framework`

Verified directly against PyPI metadata: `agent-framework==1.15.0`'s *only* dependency is
`agent-framework-core[all]==1.15.0`. That `[all]` extra unconditionally pulls in
`agent-framework-foundry-hosting` and pre-release `azure-ai-agentserver-*` packages -- the current
(non-retired) Foundry hosted-agent backend this package has no use for at all: Part A of this
track is MAF running standalone against a self-hosted MCP endpoint, no Azure or Foundry adapters
involved (see [`foundry/README.md`](./foundry/README.md) for the deliberately separate,
documentation-only Part B). `agent-framework-core` with no extras provides every symbol this
package actually imports (`MCPStreamableHTTPTool`, `FunctionTool`, `MiddlewareFailure`, and so on)
and resolves cleanly with ordinary, non-pre-release dependencies -- confirmed by installing it
alone in an isolated environment while building this package.

## Why no `algenta-sdk` dependency

Every package in this repository may depend on at most one Algenta-owned thing, the published
`algenta-sdk` client -- but only if it's genuinely used. This package never imports it:
`create_algenta_tools` talks to the caller's self-hosted Algenta MCP endpoint directly via
`agent_framework.MCPStreamableHTTPTool`, the same reason `typescript/algenta-tools` (D2) declares
no `algenta-sdk` dependency either. Declaring it anyway -- the way `pydantic-ai-algenta` and
`langchain-algenta` currently do, without importing it anywhere in their own source, only in
README prose describing what a caller's *own* approval callback might call -- would repeat
exactly the leftover-placeholder-dependency pattern an adversarial review is on record catching
elsewhere in this repository's D2/D4 history. If this package ever needs a real, direct
`algenta-sdk` call (for example, a convenience helper that records an out-of-band approval the
way `pydantic_ai_algenta.resume.approve_and_resume` documents doing by hand), that's the point to
add the dependency, not before.

## What was and wasn't verified about .NET compatibility

The plan this track was scoped against says MAF's cross-language surface should be "verified via
MAF's cross-language API." That phrase doesn't survive contact with a Python-only research pass:
a `pip install agent-framework-core` cannot exercise a `dotnet/` tree at all. What *is* honestly
confirmed: `agent-framework-core`'s own PyPI metadata points `source` at
`https://github.com/microsoft/agent-framework/tree/main/python`, and an unauthenticated GitHub API
call against that repository's root shows real top-level `python/`, `dotnet/`, and `go/`
directories -- a genuine multi-language monorepo, not a Python-only project dressed up with a
misleading name. Beyond that structural fact, no `dotnet/` source was inspected, and no wire-level
or API-level cross-language parity mechanism was found referenced anywhere in the installed Python
package. Treat any ".NET parity verified" claim about this package as unverified until someone
separately inspects the `dotnet/` tree -- this package does not fabricate that verification.

## Typed receipts

Every governed Algenta MCP tool call's result payload is parseable into a
`GovernedExecutionReceipt`:

```python
from maf_algenta import GovernedExecutionReceipt, parse_receipt
from maf_algenta.toolset import _extract_function_result_payload

# `function_result_content` is a real `agent_framework.Content(type="function_result")` item
# taken off an `agent.run()` result's `.messages` -- the same shape a real chat model would see.
payload = _extract_function_result_payload([function_result_content])
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

## Testing this package's own test suite (not your agent)

The test suite (`tests/`) runs a real
[`mcp.server.fastmcp.FastMCP`](https://modelcontextprotocol.io/) server (the base MCP SDK's own
FastMCP) over a real local HTTP socket -- a deliberately fake, deterministic stand-in for a
self-hosted Algenta MCP endpoint, never a real engine (none is reachable in CI) -- and drives it
with the real `create_algenta_tools` -> `MCPStreamableHTTPTool` -> `mcp` client -> wire round trip.
The model side of the loop is driven by `tests/fake_chat_client.py`'s `FakeChatClient`: a real
`agent_framework._clients.BaseChatClient` subclass composed with the real `FunctionInvocationLayer`
/ `ChatMiddlewareLayer` / `ChatTelemetryLayer` mixins (the same technique as pydantic-ai's
`TestModel` or LangChain's `FakeListChatModel` -- `agent_framework` 1.15.0 ships no built-in test
double of its own, confirmed directly), so the real approval-gate / function-invocation loop runs,
with only the "what does the model say next" decision scripted and zero network egress.

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest maf-algenta -v
```

This repository's CI runs exactly that isolated command per package (never `pytest .` across
multiple packages at once) -- two packages sharing a `tests/__init__.py` module name would
otherwise collide in one shared invocation.
