# haystack-algenta

[![PyPI](https://img.shields.io/pypi/v/haystack-algenta.svg)](https://pypi.org/project/haystack-algenta/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../README.md)

Haystack tool integration for [Algenta](https://algenta.ai): `create_algenta_tools`, a
governed-execution-aware `haystack.tools.toolset.Toolset` wrapping your own self-hosted Algenta
engine's MCP tool surface via Haystack's own real
[`MCPToolset`](https://docs.haystack.deepset.ai/docs/mcptoolset) (a separate PyPI package,
`mcp-haystack` -- see [Why `mcp-haystack`, not just `haystack-ai`](#why-mcp-haystack-not-just-haystack-ai)),
plus `build_algenta_governance_hooks`, an `Agent` `after_tool` hook that raises a typed
`AlgentaToolDenied` for `execute_decision`'s real, synchronous denial, via Haystack's own real
`after_tool` hook seam.

- **Tool-profile filtering** -- expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json).
  This is Haystack's own native `MCPToolset(tool_names=...)` mechanism -- no wrapper needed for
  this half at all.
- **Two-layer never-model-facing scrubbing** -- `force`/`override_safety` are stripped from every
  returned tool's advertised schema *and* from the arguments dict actually forwarded to the real
  call.
- **Typed `execute_decision` outcomes** -- parseable via `extract_execution_outcome_from_tool_result`
  into either a real `ExecutionReceipt` (success) or an `ExecutionBlocked` (one of the three real
  named-gate denials).
- **An honest denial mapping** -- see [Denial mapping](#denial-mapping) below for the full
  accounting of what Haystack's real primitives do and don't guarantee, verified live against
  every `haystack-ai` / `mcp-haystack` version this package's `pyproject.toml` permits, not
  assumed from documentation.

## Prerequisites

Before you install anything, you need:

- **A running self-hosted Algenta engine**, reachable over MCP. There is no Algenta-hosted cloud
  service to point at instead -- see [Self-hosted-first](#self-hosted-first) below. No engine
  running yet? Skip to [Try it locally](#try-it-locally-no-live-engine-required) below, which
  needs nothing but this package's own test dependencies.
- **Python 3.10 or newer.**
- **An API key for whatever chat-completion model you drive your agent with** (e.g.
  `OPENAI_API_KEY` for the `OpenAIChatGenerator` used in [Quick start](#quick-start) below). This
  is a key for your model provider, not for Algenta -- this package and the engine it talks to
  never require an API key of their own.

## Install

```bash
pip install haystack-algenta
```

This package depends on **`haystack-ai`** and **`mcp-haystack`** (both real, non-optional runtime
dependencies) and deliberately **not** on `algenta-sdk` -- see
[Why no `algenta-sdk` dependency](#why-no-algenta-sdk-dependency).

## Self-hosted-first

`create_algenta_tools` talks to **your own self-hosted Algenta engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `base_url=` passed to the function,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

Requires `OPENAI_API_KEY` in your environment (see [Prerequisites](#prerequisites)) and a running
self-hosted Algenta engine at the `base_url` below:

```python
from haystack.components.agents import Agent
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from haystack_algenta import create_algenta_tools

toolset = create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")
agent = Agent(chat_generator=OpenAIChatGenerator(), tools=toolset)
result = agent.run(messages=[ChatMessage.from_user("What's the expected value of scenario X?")])
print(result["messages"][-1].text)
toolset.close()  # tears down the MCP connection this call built
```

If no engine is listening at `base_url`, this raises `MCPConnectionError` with a message naming
the URL it tried and the three things to check (is the URL right, is the server running, is the
auth token correct) -- there's nothing else to configure to get past it.

`profile="observe"` is also the default if you omit it -- the agent can call `get_contract` /
`query_data` / `simulate` / `recommend`, and nothing that plans, logs, or executes a decision.

**Unlike this repository's other four Python siblings, everything here is synchronous.**
`MCPToolset`'s real, public API (`warm_up()`, `Tool.invoke()`, `Agent.run()`) is itself
synchronous -- it bridges the real async MCP client through its own internal `AsyncExecutor` --
so `create_algenta_tools` is a plain function, not an `async def`, and there is no `pytest-asyncio`
anywhere in this package's own test suite. A genuine framework-level difference from
`pydantic-ai-algenta`/`langchain-algenta`/`litellm-algenta`/`maf-algenta`, not an oversight.

## Try it locally (no live engine required)

Want to see `create_algenta_tools` and `build_algenta_governance_hooks` run end to end before
you've stood up an engine or an LLM API key? This package's own test suite carries two small,
real, non-mocked pieces you can drive yourself:

- `tests/stub_server.py` -- a real `mcp.server.fastmcp.FastMCP` server, on a real local HTTP
  socket, implementing the same MCP tool surface a real Algenta engine exposes.
- `tests/fake_chat_generator.py` -- a real Haystack `ChatGenerator` component whose replies come
  from a scripted queue instead of a network call, so no model API key is needed either.

From a checkout of this repository:

```bash
cd python
uv sync --all-packages --all-extras
```

Then run this script (it imports the two test modules above, so it has to run from inside
`python/haystack-algenta`, the same way `pytest` does):

```python
# python/haystack-algenta/try_it_locally.py
from haystack.components.agents import Agent
from haystack.dataclasses import ChatMessage

from haystack_algenta import create_algenta_tools
from tests.fake_chat_generator import ScriptedChatGenerator, text_reply, tool_call_reply
from tests.stub_server import StubServerFixture

with StubServerFixture() as stub:
    toolset = create_algenta_tools(base_url=stub.base_url, profile="observe")
    chat_generator = ScriptedChatGenerator(
        scripted_replies=[
            tool_call_reply("query_data", {"dataset": "orders"}),
            text_reply("Found 2 rows in the orders dataset."),
        ]
    )
    agent = Agent(chat_generator=chat_generator, tools=toolset)
    result = agent.run(messages=[ChatMessage.from_user("Look up the orders dataset.")])
    toolset.close()

print(result["messages"][-1].text)
```

```bash
uv run python haystack-algenta/try_it_locally.py
```

This really does open a local HTTP socket, run a real `MCPToolset` client against it, and drive a
real `Agent` step loop -- the only things scripted are the two ends a live demo can't have without
an account: the model's replies and the engine itself. Prints:

```
Found 2 rows in the orders dataset.
```

Swap the scripted `tool_call_reply`/`text_reply` calls for your own scenario, or point
`create_algenta_tools` at `stub.base_url` from your own script, to explore the rest of this
package (including `profile="execute"` and the denial gates below) without touching a real engine.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution; see [Denial mapping](#denial-mapping). |
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

## The real `execute_decision` contract

`execute_decision(decision_id, webhook_url, timeout_seconds=None, force=None,
override_safety=None, metadata=None)` either:

- **succeeds (200)** with a real `ExecutionReceipt`: `{decision_id, webhook_url, execution_status:
  "delivered"|"failed", response_code, executed_at, policy_snapshot_id, schema_snapshot_id,
  manifest_version, payload_summary, safety_overridden}` -- note `execution_status` describes
  whether the *webhook delivery itself* succeeded, not a policy verdict; a `"failed"` delivery is
  still a completed, non-gated call, or
- **is blocked, in that same call (409)**, by exactly one of three real, named policy gates:
  `"idempotency"` (this `decision_id` was already delivered; bypassable only via `force=True`, for
  one re-execution), `"confidence"` (below `policy.min_confidence`), or `"risk_floor"` (`risk_p5`
  below `-policy.risk_floor`) -- both of the latter bypassable only via `override_safety=True`.
  The body is `{"error": {"code": "execution_blocked_<gate>", "gate": "<gate>", "message": "...",
  "override_hint": "..."}}`.

There is no third outcome. `decision_id` comes from a prior real `log_decision` call, never from a
model-facing `plan_hash` -- there is no `plan_hash` field anywhere on this tool, and no
asynchronous "pending approval" state to poll or resume. A genuinely separate, real
plan/nonce-based human-approval system does exist in the engine, but it is intentionally not
exposed as an MCP/LLM tool -- it is out of reach for this package, or any MCP-based integration,
and this package makes no claim otherwise.

## Denial mapping

Verified live against both ends of the version range this package's `pyproject.toml` permits --
`haystack-ai` 3.0.0 and 3.1.1, `mcp-haystack` 1.4.1 and 1.5.1 (a real `mcp.server.fastmcp.FastMCP`
stub server, real HTTP wire, real `Agent` run loop -- see `tests/test_toolset_scenarios.py`), not
assumed from documentation.

**`GovernedReceiptHook` -- this package's own `after_tool` hook.** Parses the real outcome out of
the tool-result message(s) `Agent._run_step` just wrote into `state.data["messages"]`
(`haystack_algenta.receipts.extract_execution_outcome_from_tool_result`), and raises
`AlgentaToolDenied` -- naming the real gate (`error.gate`, one of `"idempotency"`, `"confidence"`,
`"risk_floor"`) plus the engine's own `message`/`override_hint` -- for the real 409 denial shape. A
successful `ExecutionReceipt` and any non-`execute_decision` tool's result are both left alone
(detected by *shape*, not by tool name: nothing else this package can expose validates as either
`ExecutionReceipt` or `ExecutionBlocked`).

Proven live that this exception propagates out of `agent.run()` **completely unmodified** --
because `Agent._run_step`'s `_run_hooks(self.hooks, AFTER_TOOL, state)` call has no surrounding
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
Raising `AlgentaToolDenied` from inside `haystack_algenta.toolset`'s wrapped calls would therefore
be exactly the wrong seam for the common, real-`Agent` case -- proven, not assumed
(`tests/test_toolset_scenarios.py::test_a_blows_up_tool_error_is_wrapped_and_swallowed_by_default`
reproduces this with a real server-side exception, unrelated to the gate mapping, to isolate the
claim).

**There is no pre-call gate here, and none is needed.** An earlier version of this package also
wired Haystack's real `ConfirmationHook`/`BlockingConfirmationStrategy` as a `before_tool` gate,
bridging to an `AlgentaApprovalStillPending` abort if the engine's own (fictional) out-of-band plan
approval hadn't landed by the time the model's call went through. `execute_decision` has no
asynchronous approval state to bridge to -- a call is a same-response success or a same-response
named-gate denial, never "pending, come back later" -- so that whole pre-call/post-call bridge is
gone, not merely dormant. **Haystack's `ConfirmationHook` itself is unaffected and still a
perfectly real, general-purpose primitive** -- a caller who wants a human to confirm the model's
*request* to call `execute_decision` at all, for their own reasons unrelated to this contract, can
still wire it directly:

```python
from haystack.hooks.human_in_the_loop import AlwaysAskPolicy, BlockingConfirmationStrategy, ConfirmationHook

gate = ConfirmationHook(
    confirmation_strategies={
        "execute_decision": BlockingConfirmationStrategy(confirmation_policy=AlwaysAskPolicy(), confirmation_ui=your_ui)
    }
)
agent = Agent(chat_generator=..., tools=toolset, hooks={"before_tool": [gate], **build_algenta_governance_hooks()})
```

`tests/test_toolset_scenarios.py::test_a_confirmation_hook_can_still_be_wired_directly_for_a_pre_call_gate`
proves this composition live -- with a UI that rejects, the real MCP server is genuinely never
contacted at all. This package just doesn't build or opinionate over that wiring itself anymore,
because doing so would imply a bridge to an approval state that doesn't exist on the real tool.

### Why this package's mapping lives where it does

`haystack-algenta` surfaces the denial from a dedicated `after_tool` hook because that is the one
seam in Haystack's `Agent` loop proven to let a custom exception type survive `agent.run()` intact
-- `Tool.invoke()`'s own exception handling is too aggressive (see above) for the mapping to live
any closer to the real MCP call itself. Each of this repository's other framework packages maps
`execute_decision`'s real outcome onto whatever *that* framework's own native tool-failure idiom
is, at whichever seam that framework actually supports -- this section only speaks for this
package's own, verified-live reasoning, not for any sibling's current implementation.

### A `Pipeline`, or any direct `Tool.invoke()` caller, gets no hook at all

Hooks are an `Agent`-loop concept. `build_algenta_governance_hooks` only helps agents built with
`haystack.components.agents.Agent`. For a `Pipeline`, or any caller invoking `Tool.invoke()`/
`invoke_async()` directly, `haystack_algenta.extract_execution_outcome_from_tool_result` is the
same parsing `GovernedReceiptHook` uses, exposed directly so you can call it yourself on whatever
`invoke()` returned and decide what to do:

```python
from haystack_algenta.receipts import ExecutionBlocked, extract_execution_outcome_from_tool_result

raw_result = execute_decision_tool.invoke(decision_id="...", webhook_url="...")
outcome = extract_execution_outcome_from_tool_result(raw_result)
if isinstance(outcome, ExecutionBlocked):
    ...  # your own handling -- outcome.gate is one of "idempotency"/"confidence"/"risk_floor"
```

This is an honest, undisguised capability, not a fabricated approval-pause mechanism -- the same
stance `litellm_algenta` documents for its own no-pause gateway path.

## Why the double-JSON unwrap

Verified live: `Tool.invoke()` for an `MCPToolset`-built tool returns the raw MCP
`CallToolResult`, JSON-*serialized*, with the actual tool payload nested as a JSON *string* inside
a text content block inside that JSON:

```json
{"meta": null, "content": [{"type": "text", "text": "{\n  \"decision_id\": \"decision-1\", ... \"execution_status\": \"delivered\", ...}"}], "structuredContent": null, "isError": false}
```

Neither `Tool` nor `MCPToolset` ever unwraps this -- there is zero receipt concept anywhere in
Haystack's own tool-calling layer, so a real `execute_decision` outcome and `get_contract`'s plain
discovery blob are handled completely identically (both are just opaque strings) until something
-- this package's `unwrap_mcp_tool_result` -- peels both string layers back into a real Python
value.

## Why `mcp-haystack`, not just `haystack-ai`

`MCPToolset` is **not part of `haystack-ai`** -- confirmed by grepping an installed `haystack-ai`
tree (checked on both 3.0.0 and 3.1.1) for any `mcp` module: none exists there. It ships in a
separate PyPI package, **`mcp-haystack`**, under the `haystack_integrations.tools.mcp` import
namespace. `haystack-ai` core only has the generic `Tool`/`Toolset`/`ComponentTool`/`Agent`/hooks
primitives this package also uses. Any consumer of this package gets both transitively (they're
both real, non-optional dependencies here), but it's worth knowing they're two separate packages
if you're pinning versions yourself.

## Why no `algenta-sdk` dependency

Every package in this repository may depend on at most one Algenta-owned thing, the published
`algenta-sdk` client -- but only if it's genuinely used. This package never imports it:
`create_algenta_tools` talks to the caller's self-hosted Algenta MCP endpoint directly via
`haystack_integrations.tools.mcp.MCPToolset`, the same reason `maf-algenta` and
`typescript/algenta-tools` declare no `algenta-sdk` dependency either. Declaring it anyway,
without importing it anywhere in this package's own source, would repeat exactly the
leftover-placeholder-dependency pattern an adversarial review is on record catching elsewhere in
this repository's history.

## Not a `WrapperToolset`/interceptor

Every real, in-process sibling in this repository (`pydantic-ai-algenta`, `langchain-algenta`,
`maf-algenta`) builds one wrapper object that filters, scrubs, *and* maps denials in a single
seam. This package deliberately does not: `haystack_algenta.toolset` only does profile filtering
(native, via `MCPToolset(tool_names=...)`) and the two-layer scrub; `haystack_algenta.hooks` is a
separate, `Agent`-level hook. Haystack genuinely has no single interception point on
`MCPToolset`/`Tool` to build a combined wrapper on top of -- `Toolset`'s entire public surface is
`add`/`from_dict`/`get_selectable_tools`/`spawn`/`to_dict`/`warm_up`, and `MCPToolset` builds each
`Tool`'s call as a closure with no `tool_interceptors=`-style hook anywhere (grep-verified against
the installed package). Rebuilding each `Tool` via `dataclasses.replace` (what this package does
for the scrub) is the same escape-hatch pattern `langchain_algenta._wrap_plain_tool` and
`maf_algenta._wrap_mcp_function` already use for their own non-native-seam cases; the denial
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
