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
- **A typed `execute_decision` receipt/denial mapping** -- a successful call returns a real
  [`ExecutionReceipt`][receipt]; a call the real engine blocks synchronously (its real HTTP 409,
  one of three named policy gates) raises a typed `AlgentaToolDenied` built on
  `agent_framework.MiddlewareFailure` (MAF's one fail-closed abort signal) -- see
  [Denial mapping](#denial-mapping) for the full, honest accounting, including what an earlier
  version of this package got wrong.

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
| `execute` | + `execute_decision` | Real-world execution; synchronous success/denial -- see [Denial mapping](#denial-mapping). |
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
call supplying `{"decision_id": ..., "webhook_url": ..., "force": true}` against a schema with
`force` already removed sails straight through validation, and the real underlying
`execute_decision` call would have received `force=True` if the call-time scrub weren't also
there. `tests/test_never_model_facing.py`
and `tests/test_toolset_scenarios.py::test_smuggled_force_never_reaches_the_real_server_over_the_real_wire`
both assert on the real received arguments (via the stub server's own `forced` echo field), not
just on the advertised schema.

## Denial mapping

**The real contract, verified directly against the live, running Algenta Engine's own source**
(not assumed from any planning document or from this package's own prior README, both of which
turned out to describe a fictional contract): `execute_decision(decision_id, webhook_url,
timeout_seconds?, force?, override_safety?, metadata?)` either

- succeeds synchronously (HTTP 200): a real
  [`ExecutionReceipt`](./maf_algenta/receipts.py) -- `decision_id`, `webhook_url`,
  `execution_status` (`"delivered"` or `"failed"` -- the *webhook delivery* outcome, not a
  governance verdict; even a `"failed"` delivery is a completed, successful call with a real
  receipt, no exception raised), `response_code`, `executed_at`, `policy_snapshot_id`,
  `schema_snapshot_id`, `manifest_version`, `payload_summary`, `safety_overridden`; or
- is blocked synchronously (HTTP 409), in the very same call, with a body shaped
  `{"error": {"code": "execution_blocked_<gate>", "gate": "idempotency" | "confidence" |
  "risk_floor", "message": ..., "override_hint": ...}}`.

**There is no third state.** No `plan_hash`, no `approval_state`, and critically, no asynchronous
"pending" outcome exists anywhere on this tool in the real engine. A call either succeeds or is
denied, both synchronously, in the same call -- never "pending, come back later."

**An earlier version of this package modeled a fictional contract:** it assumed `execute_decision`
carried an async, `plan_hash`-keyed `approval_state` (`"none"` / `"pending"` / `"approved"` /
`"rejected"` / `"expired"`), gated the call with MAF's `approval_mode="always_require"` pre-call
primitive as if the model needed permission to *attempt* the call, and then still had to invent a
fail-closed `AlgentaApprovalStillPending` exception for the receipt coming back `"pending"` after
that gate passed -- because the two states were never actually connected to anything real. None of
that exists on the real tool, so none of it is modeled here anymore: `execute_decision` no longer
carries `approval_mode="always_require"` at all (there is nothing for a human to approve *before*
the call -- only a real, synchronous outcome to observe *from* it), and
`AlgentaApprovalStillPending` no longer exists.

**What actually happens now, verified live against the installed `agent-framework-core` 1.15.0:**
a real HTTP 409 denial reaches this package as an MCP tool-error result. `agent_framework`'s own
MCP client (`agent_framework._mcp.MCPStreamableHTTPTool`) raises
`agent_framework.exceptions.ToolExecutionException` whenever the underlying `CallToolResult` comes
back `isError=True` -- confirmed by reading the installed source directly, not assumed. This
package catches exactly that exception around the real `execute_decision` call, recovers the real
`{"error": {...}}` body out of its text (the real MCP server framework wraps *any* exception a
tool raises as `f"Error executing tool {name}: {e}"` before it becomes that text -- confirmed
against the installed `mcp` SDK -- so the JSON body is recovered by scanning for its first `{`,
not by assuming the whole message is JSON), and re-raises it as `AlgentaToolDenied`: still built
on `agent_framework.MiddlewareFailure` (MAF's one real fail-closed abort primitive -- "the loop's
explicit fail-closed escape: it is never converted into a tool result, ... and the exception
propagates to the caller of `Agent.run`," quoted directly from the installed package's own
docstring, and verified live to propagate unmodified even with zero middleware registered on the
agent), so `isinstance(exc, agent_framework.MiddlewareFailure)` is still always true for anything
this package raises, and a real denial still can't silently be swallowed into a tool-error result
the model then sees and might paper over.

`AlgentaToolDenied.blocked` carries the parsed `ExecutionBlocked` detail: `blocked.gate` is one of
the three real gate names, `blocked.message`, and `blocked.override_hint`. `force=true` bypasses
only the `"idempotency"` gate (a decision already delivered), for one re-execution;
`override_safety=true` bypasses only `"confidence"`/`"risk_floor"`. Neither field is ever
model-facing (see [Why two layers, verified](#why-two-layers-verified)) -- resolving a real denial
means a human operator decides whether to retry the call with one of them set, outside the
model-facing tool surface entirely.

A non-error result that doesn't validate as a real `ExecutionReceipt` (a genuine anomaly -- an
engine bug, or a version skew this package hasn't caught up with yet) raises
`AlgentaToolExecutionFailed` instead, for the same reason: the real contract says a non-error
`execute_decision` result is always a real receipt, so anything else is worth failing loudly on
rather than passing through as if it were fine. Both `AlgentaToolDenied` and
`AlgentaToolExecutionFailed` inherit `AlgentaGovernedCallFailure`, which inherits
`agent_framework.MiddlewareFailure`.

Every other tool this package wraps (`get_contract`, `query_data`, `simulate`, `recommend`,
`plan_decision`, `log_decision`) has no verified receipt/denial contract of its own in the real
engine, so this package imposes none on them -- their results pass straight through, scrubbed but
otherwise unexamined. Only `execute_decision` gets this typed mapping.

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

A successful `execute_decision` call's result payload is parseable into a real `ExecutionReceipt`
(this package already does this internally -- `create_algenta_tools` raises before you'd ever see
an unparseable one -- but the parser is public for a caller who wants to work with the payload
directly, e.g. after pulling it back out of a logged tool-call transcript):

```python
from maf_algenta import ExecutionReceipt, parse_execution_receipt
from maf_algenta.toolset import _extract_function_result_payload

# `function_result_content` is a real `agent_framework.Content(type="function_result")` item
# taken off an `agent.run()` result's `.messages` -- the same shape a real chat model would see.
payload = _extract_function_result_payload([function_result_content])
receipt: ExecutionReceipt | None = parse_execution_receipt(payload)
if receipt is not None:
    receipt.decision_id
    receipt.webhook_url
    receipt.execution_status    # "delivered" | "failed" -- the webhook delivery outcome
    receipt.response_code
    receipt.executed_at
    receipt.policy_snapshot_id
    receipt.schema_snapshot_id
    receipt.manifest_version
    receipt.payload_summary
    receipt.safety_overridden
    receipt.is_delivered()      # execution_status == "delivered"
```

A real denial (see [Denial mapping](#denial-mapping)) is caught with `AlgentaToolDenied`, whose
`.blocked` attribute is the parsed `ExecutionBlocked` detail:

```python
from maf_algenta import AlgentaToolDenied

try:
    await execute_decision.invoke(arguments={"decision_id": "...", "webhook_url": "..."})
except AlgentaToolDenied as exc:
    exc.blocked.gate            # "idempotency" | "confidence" | "risk_floor"
    exc.blocked.code            # "execution_blocked_<gate>"
    exc.blocked.message
    exc.blocked.override_hint
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
double of its own, confirmed directly), so the real function-invocation loop runs, with only the
"what does the model say next" decision scripted and zero network egress.

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest maf-algenta -v
```

This repository's CI runs exactly that isolated command per package (never `pytest .` across
multiple packages at once) -- two packages sharing a `tests/__init__.py` module name would
otherwise collide in one shared invocation.
