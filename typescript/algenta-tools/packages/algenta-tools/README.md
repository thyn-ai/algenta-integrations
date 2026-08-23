# algenta-tools

Vercel AI SDK (`ai` v7) tool integration for [Algenta](https://algenta.ai): `createAlgentaTools`,
a factory that connects to your own self-hosted Algenta Engine's MCP endpoint (via
[`@ai-sdk/mcp`](https://www.npmjs.com/package/@ai-sdk/mcp)'s real MCP client) and returns a
governed-execution-aware AI SDK [`ToolSet`](https://ai-sdk.dev/docs/ai-sdk-core/tools-and-tool-calling)
ready to pass to `generateText` / `streamText` / an `Agent`. It layers on:

- **Tool-profile filtering** — expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../../../contracts/integration-tool-contract.json).
- **Never-model-facing field scrubbing** — `force`/`override_safety` (operator/break-glass-only
  fields on `execute_decision`'s real schema) are stripped from every tool's advertised JSON
  Schema *and* from the arguments object actually forwarded to the wrapped MCP call, in every
  profile.
- **Typed governed-execution receipts** — a governed tool call's result is parsed into a
  [`GovernedExecutionReceipt`](./src/receipts.ts) when it validates as one, so your code gets a
  typed object instead of an untyped value.
- **A `needsApproval`-based approval flow** for `execute_decision` — see
  [The approval flow](#the-approval-flow) below for the full design write-up, including the one
  place where this is *stricter* than a naive port of the sibling Python package's approach.

## Install

```bash
npm install algenta-tools ai zod
```

`ai` (`^7.0.0`) and `zod` (`^4.0.0`) are peer dependencies — you already have them in any project
using the AI SDK. This package depends on exactly one other thing: the published
[`algenta-sdk`](https://www.npmjs.com/package/algenta-sdk) npm package (plus `@ai-sdk/mcp`, the
AI SDK's own current MCP client package — see [Why `@ai-sdk/mcp` and not
`ai`](#why-ai-sdkmcp-and-not-ai-itself) below). It never depends on, imports, or bundles any part
of the Algenta Engine itself.

## Self-hosted-first

`createAlgentaTools` talks to **your own self-hosted Algenta Engine** over its MCP endpoint —
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `baseUrl` passed to `createAlgentaTools`,
2. the `ALGENTA_BASE_URL` environment variable,
3. `http://localhost:8000/mcp` (the default for a local self-hosted engine).

## Quick start

```ts
import { streamText } from "ai";
import { createAlgentaTools } from "algenta-tools";

// Talks to your own self-hosted engine (ALGENTA_BASE_URL, or the baseUrl option below).
const tools = await createAlgentaTools({ baseUrl: "http://localhost:8000/mcp", profile: "observe" });

const result = streamText({
  model: "openai/gpt-5",
  tools,
  prompt: "What's the expected value of scenario X?",
});
```

`profile: "observe"` is also the default if you omit it — the model can call `get_contract` /
`query_data` / `simulate` / `recommend`, and nothing that writes, plans, or executes anything.
See [Tool profiles](#tool-profiles) to opt into more.

Every governed tool call's result comes back as a typed
[`GovernedExecutionReceipt`](#typed-receipts) (when the connected tool returns Algenta's
governed-execution envelope — a tool like `get_contract` that doesn't is returned unchanged), so
downstream code can do `result.result`, `result.approval_state`, etc. instead of indexing into a
raw value.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution, approval-gated — see below. |
| `full` | everything the connected engine advertises | Opt-in only; admin/ops tooling. |

```ts
const tools = await createAlgentaTools({ baseUrl: "...", profile: "execute" });
```

An `observe`-profile `ToolSet` genuinely does not contain `execute_decision` (or anything
`govern`/`execute`-tier) — it's not just undocumented, the model has no way to know it exists.
`force`/`override_safety` are never exposed either, in any profile: stripped from the advertised
JSON Schema *and* scrubbed from the arguments object actually forwarded to the wrapped MCP call,
in case something upstream still tried to pass one.

## The approval flow

`execute_decision` is real-world execution and is approval-gated. The contract already requires
its out-of-band policy approval to be recorded **before** the call is made at all — not paused
mid-call awaiting one (see `contracts/integration-tool-contract.json`'s
`profiles.execute.requires_all_of`). That maps cleanly onto AI SDK's real, pre-call gate: every
tool created by `createAlgentaTools` sets

```ts
needsApproval: name === "execute_decision" ? true : undefined
```

unconditionally — never an input-inspecting function. AI SDK will not call `execute()` for
`execute_decision` until your application's own approval flow supplies a
[`ToolApprovalResponseOutput`](https://ai-sdk.dev/docs) for that call. This is a genuinely
*stronger, earlier* gate than the sibling `pydantic-ai-algenta` package's design, which inspects
the receipt returned by an already-made call (`ApprovalRequired`, raised post-hoc from inside
`call_tool`) — here, no call reaches the wrapped MCP client at all until a human (or your policy
engine) approves it client-side first.

**The engine is still the final authority, though.** It can return `approval_state: "pending"` on
*any* governed call's receipt even after the client-side `needsApproval` gate already passed —
the client-side gate is defense-in-depth UX, never a substitute for the engine's own check.
Since AI SDK has already invoked `execute()` by the time that receipt comes back, there is no
framework primitive to retroactively pause the call the way `needsApproval` does *before* it —
so `execute()` **throws** a clear "still pending server-side policy approval" error instead of
returning the receipt as if it had succeeded (AI SDK surfaces a thrown error from `execute()` as
a `tool-error` part, which a model can see and react to, e.g. by telling the user execution is
still awaiting approval). A `"rejected"`/`"expired"` `approval_state`, or a named policy-gate
`code` (`plan_not_approved`, `stale_plan`, `plan_hash_mismatch`, `idempotency_key_conflict`), also
throw — AI SDK has no separate denied-vs-failed distinction the way `pydantic-ai-algenta` has
`ToolDenied`/`ToolFailed`, so the thrown message states plainly that the failure is a **policy
denial** rather than an ordinary error, and includes the engine's own code and message. A
successful receipt, or a non-receipt result (e.g. `get_contract`'s discovery payload), is
returned from `execute()` normally — exactly like every other tool.

This is the full mapping every wrapped tool call goes through:

| Receipt state | Result |
|---|---|
| Not a governed-execution envelope (e.g. `get_contract`) | Returned as-is |
| `approval_state: "none"` or `"approved"`, `status: "ok"`/`"success"` | Returned as a typed `GovernedExecutionReceipt` |
| `approval_state: "pending"` | Throws: still pending server-side approval |
| `approval_state: "rejected"`/`"expired"`, or a named policy-gate `code` | Throws: denied by policy, with the engine's reason |
| Anything else (a generic failure) | Throws: did not complete successfully |

## Typed receipts

```ts
import { type GovernedExecutionReceipt } from "algenta-tools";

const receipt = (await tools.query_data!.execute!(
  { dataset: "orders" },
  executionOptions,
)) as GovernedExecutionReceipt;

receipt.status;          // "ok" | "error" | ...
receipt.code;             // "ok" | "plan_hash_mismatch" | "upstream_timeout" | ...
receipt.approval_state;   // "none" | "pending" | "approved" | "rejected" | "expired"
receipt.plan_hash;
receipt.execution_id;
receipt.idempotency_key;
receipt.result;            // the tool's actual payload, once unwrapped from the envelope
```

A tool whose result *doesn't* validate as this envelope (e.g. a real `get_contract`'s discovery
payload) passes through unchanged as an ordinary result — `createAlgentaTools` doesn't assume
every tool on a self-hosted Algenta MCP endpoint returns this exact shape, only that governed
decision/execution tools do. Extra fields the engine adds over time are tolerated, not dropped —
`parseReceipt` uses a `.passthrough()` schema, so a newer engine talking to an older client isn't
penalized for sending one more field than this package knows about.

## Why `@ai-sdk/mcp` and not `ai` itself?

AI SDK's MCP client support (`experimental_createMCPClient` / `createMCPClient`) used to live
directly in the `ai` package, but — verified directly against the installed packages while
building this integration, not assumed from memory — it has since moved into its own dedicated
`@ai-sdk/mcp` package (`ai`'s own `dist/index.d.ts` no longer exports an MCP client at all in the
version this package was built and tested against). `@ai-sdk/mcp`'s own changelog documents the
migration explicitly: `import { experimental_createMCPClient } from "ai"` becomes `import {
experimental_createMCPClient } from "@ai-sdk/mcp"`. This package depends on `@ai-sdk/mcp`
directly rather than assuming it ships bundled inside `ai`.

## Why not reuse `algenta-sdk`'s `MCP_ENDPOINT`/`DEFAULT_BASE_URL` constants?

The published `algenta-sdk` package exports `DEFAULT_BASE_URL` (`https://api.algenta.ai`) and
`MCP_ENDPOINT` (`https://api.algenta.ai/mcp`) — checked directly against the installed package
while building this integration. Both point at Algenta's **hosted cloud** endpoint, which is
exactly the default this package must never use (see [Self-hosted-first](#self-hosted-first)
above and the contract's `self_hosted_only_note`). `createAlgentaTools` therefore defines its own
`DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"`, matching the sibling
`pydantic-ai-algenta` package's own default, instead of importing a constant from `algenta-sdk`
that would silently point every unconfigured caller at Algenta's cloud.

## Why a factory function and not a class?

The sibling Python package exposes `AlgentaToolset`, a `pydantic_ai.toolsets.wrapper.WrapperToolset`
subclass, because pydantic-ai's own tool-calling surface is built around a `Toolset` abstraction
that frameworks are expected to subclass. AI SDK has no equivalent abstraction — a `ToolSet` is
just `Record<string, Tool>`, and the idiomatic way every real MCP integration in this ecosystem
(including this org's own `@thyn-ai/sqai` `ai-sdk` package) exposes its tools is a plain factory
function returning that record, not a class wrapping one. `createAlgentaTools` follows that
convention rather than inventing a parallel "toolset" concept AI SDK doesn't have.

## Advanced: bringing your own MCP client or tool set

```ts
import { connectAlgentaMCPClient, createAlgentaTools } from "algenta-tools";

// Reuse an already-connected client across multiple profiles/calls, or configure it yourself
// (custom headers, an OAuth provider, maxRetries, ...).
const client = await connectAlgentaMCPClient({ baseUrl: "http://localhost:8000/mcp" });
const observeTools = await createAlgentaTools({ client, profile: "observe" });
const executeTools = await createAlgentaTools({ client, profile: "execute" });
// ... later: await client.close();
```

`tools` (a pre-built `Record<string, Tool>`) is also accepted instead of any connection option,
for wrapping a hand-built or already-fetched tool set without talking to MCP at all — mirroring
`AlgentaToolset`'s `wrapped=` escape hatch on the Python side. `tools` and the connection options
(`client`/`baseUrl`/`headers`/`mcpClientConfig`) are mutually exclusive.

## Testing this package's own test suite (not your agent)

The test suite (`src/*.test.ts`) runs a real
[`@modelcontextprotocol/sdk`](https://www.npmjs.com/package/@modelcontextprotocol/sdk) `McpServer`
over a real local `node:http` socket (`src/test-support/stub-server.ts`) — a deliberately fake,
deterministic stand-in for a self-hosted Algenta MCP endpoint, never a real engine (none is
reachable in CI) — and drives it with a real `@ai-sdk/mcp` `MCPClient` and a real
`createAlgentaTools`, over the real wire. Profile-filtering and schema/argument-scrubbing unit
tests additionally use an in-memory fake `ToolSet` (via the `tools` option above) where a network
round trip isn't the thing under test.

```bash
cd typescript/algenta-tools
pnpm install
pnpm --filter algenta-tools test
```
