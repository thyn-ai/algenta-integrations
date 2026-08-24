# algenta-tools

Vercel AI SDK (`ai` v7) tool integration for [Algenta](https://algenta.ai): `createAlgentaTools`,
a factory that connects to your own self-hosted Algenta Engine's MCP endpoint (via
[`@ai-sdk/mcp`](https://www.npmjs.com/package/@ai-sdk/mcp)'s real MCP client) and returns an
Algenta-aware AI SDK [`ToolSet`](https://ai-sdk.dev/docs/ai-sdk-core/tools-and-tool-calling)
ready to pass to `generateText` / `streamText` / an `Agent`. It layers on:

- **Tool-profile filtering** — expose only `observe` (read-only, the default), `govern`,
  `execute`, or the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../../../contracts/integration-tool-contract.json).
- **Never-model-facing field scrubbing** — `force`/`override_safety` (operator/break-glass-only
  fields on `execute_decision`'s real schema) are stripped from every tool's advertised JSON
  Schema *and* from the arguments object actually forwarded to the wrapped MCP call, in every
  profile.
- **`execute_decision`'s typed success/denial contract** — a successful call is parsed into a
  typed [`ExecutionReceipt`](./src/receipts.ts); a blocked call surfaces as a typed
  [`ExecutionBlockedError`](./src/receipts.ts) thrown from `execute()`, carrying the engine's real
  named safety gate. See [Executing a decision](#executing-a-decision) below for the full mapping.

## Install

```bash
npm install algenta-tools ai zod
```

`ai` (`^7.0.0`) and `zod` (`^4.0.0`) are peer dependencies — you already have them in any project
using the AI SDK. This package's only real dependency is
[`@ai-sdk/mcp`](https://www.npmjs.com/package/@ai-sdk/mcp), the AI SDK's own current MCP client
package (see [Why `@ai-sdk/mcp` and not `ai`](#why-ai-sdkmcp-and-not-ai-itself) below). It does
**not** depend on the published `algenta-sdk` npm package — that package is a thin HTTP/gRPC
client for Algenta's REST-shaped API surface (datasets, runtime, simulations), not an MCP client,
so it has nothing this package's MCP-protocol tool-calling path would use (see [Why not reuse
`algenta-sdk`'s `MCP_ENDPOINT`/`DEFAULT_BASE_URL`
constants](#why-not-reuse-algenta-sdks-mcp_endpointdefault_base_url-constants) below for the one
overlap that was considered and rejected). This package never depends on, imports, or bundles any
part of the Algenta Engine itself.

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

Every other tool's result is returned as its own real, freeform response body (a plain
pass-through). `execute_decision` is the one exception — see [Executing a
decision](#executing-a-decision) below.

## Tool profiles

| Profile | Adds | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution — see below. |
| `full` | everything the connected engine advertises | Opt-in only; admin/ops tooling. |

```ts
const tools = await createAlgentaTools({ baseUrl: "...", profile: "execute" });
```

An `observe`-profile `ToolSet` genuinely does not contain `execute_decision` (or anything
`govern`/`execute`-tier) — it's not just undocumented, the model has no way to know it exists.
`force`/`override_safety` are never exposed either, in any profile: stripped from the advertised
JSON Schema *and* scrubbed from the arguments object actually forwarded to the wrapped MCP call,
in case something upstream still tried to pass one.

## Executing a decision

`execute_decision(decision_id, webhook_url, timeout_seconds?, force?, override_safety?, metadata?)`
dispatches one already-logged decision (from `log_decision`) for real-world execution — a webhook
delivery. There is no separate approval-pending state to wait out: the real engine's call is
**synchronous**. It either:

- **Succeeds (200)** and returns a real `ExecutionReceipt` — `decision_id`, `webhook_url`,
  `execution_status` (`"delivered"` | `"failed"`), `response_code`, `executed_at`,
  `policy_snapshot_id`, `schema_snapshot_id`, `manifest_version`, `payload_summary`,
  `safety_overridden` — which `createAlgentaTools` parses and returns from `execute()` like any
  other successful tool call, or
- **Is blocked**, in the same call, naming exactly one of three real safety gates:
  - `"idempotency"` — this `decision_id` was already delivered; bypassable only by a human
    operator passing `force: true` for one re-execution,
  - `"confidence"` — the logged decision's confidence is below `policy.min_confidence`,
  - `"risk_floor"` — the logged decision's `risk_p5` is below `-policy.risk_floor`,

  the last two bypassable only by a human operator passing `override_safety: true`. Neither
  `force` nor `override_safety` is ever model-facing — see [Tool
  profiles](#tool-profiles) above.

A blocked call throws an [`ExecutionBlockedError`](./src/receipts.ts) from `execute()` — the same
native "tool call failed" idiom every other tool-level error in this package already goes
through (AI SDK surfaces a thrown error as a `tool-error` part). `ExecutionBlockedError` carries
the engine's real `gate` (`"idempotency"` | `"confidence"` | `"risk_floor"`), `code` (e.g.
`"execution_blocked_confidence"`), and `overrideHint`, so your code can branch on `error.gate`
directly instead of re-parsing `error.message`:

```ts
import { ExecutionBlockedError } from "algenta-tools";

try {
  const receipt = await tools.execute_decision!.execute!(
    { decision_id, webhook_url },
    executionOptions,
  );
} catch (error) {
  if (error instanceof ExecutionBlockedError) {
    // error.gate: "idempotency" | "confidence" | "risk_floor"
    // error.code, error.overrideHint carry the engine's own values verbatim.
  }
}
```

There is no approval-pause state here, so `createAlgentaTools` sets no `needsApproval` (or any
other pause/resume gate) on `execute_decision` — same as every other tool. A prior version of
this package modeled an approval-pending flow keyed on a `plan_hash`/`approval_state` shape;
that shape does not exist anywhere on the real `execute_decision` tool, and has been removed.

This is the full mapping `execute_decision` goes through:

| Real engine response | Result |
|---|---|
| 200, a valid `ExecutionReceipt` | Returned as a typed `ExecutionReceipt` |
| Blocked on a named gate (`idempotency` / `confidence` / `risk_floor`) | Throws `ExecutionBlockedError`, with `gate`/`code`/`overrideHint` |
| Any other tool-error result | Throws a plain `Error` |

Every other tool's result — `plan_decision`'s `DecisionPlan` summary, `log_decision`'s
`{decision_id, chosen_action, expected_value, confidence, created_at, note}`, `query_data`'s rows,
etc. — is returned from `execute()` unchanged; none of them share `execute_decision`'s
receipt/denial shape, so this package doesn't try to parse them into it.

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
that would silently point every unconfigured caller at Algenta's cloud. This was the only thing
`algenta-sdk` had that overlapped with this package's job at all, and it turned out to be exactly
the wrong default — so this package does not declare `algenta-sdk` as a dependency (see
[Install](#install) above).

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
