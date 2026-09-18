# n8n-nodes-algenta

[![npm](https://img.shields.io/npm/v/n8n-nodes-algenta.svg)](https://www.npmjs.com/package/n8n-nodes-algenta)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../../../README.md)

An [n8n](https://n8n.io) community node for [Algenta](https://algenta.ai): a governed-execution-aware
node exposing your own self-hosted Algenta Engine's MCP tool surface as typed n8n operations —
`Get Contract`, `Query Data`, `Simulate`, `Recommend`, `Plan Decision`, `Log Decision`, and
`Execute Decision` — with a real `ExecutionReceipt` / named-safety-gate contract on the last one.

n8n already ships a generic, built-in `MCP Client` node that can call any MCP server, including
Algenta's. This package is not a replacement for that — it exists for the value a generic node
can't give you: typed per-operation parameters instead of a hand-built JSON blob, a proper
`ExecutionReceipt` in a successful Execute Decision's output, a `NodeApiError` naming the real
policy gate (`idempotency` / `confidence` / `risk_floor`) when one blocks it, and a `Tool Profile`
selector that refuses `Execute Decision` outright under `observe`/`govern` — enforced *before* any
network call, including when this node is wrapped as a tool inside an n8n AI Agent.

## Prerequisites

Before doing anything below, have these ready:

- **Node.js 18.10+** — and **pnpm 9+** as well, only if you build or develop this package from
  source instead of installing the published npm package.
- **A running self-hosted Algenta Engine**, reachable over HTTP — for example `http://localhost:8000`.
  This node never talks to an Algenta-hosted cloud endpoint; there isn't one to point it at. If you
  don't have an engine running yet, see [Self-host the engine](https://docs.algenta.ai/deploy-and-operate/self-hosting).
- **An API key**, only if your engine enforces authentication. Engines running with auth disabled
  (e.g. local development) don't need one.
- **n8n itself**, if you want to see the node running inside the n8n editor rather than just
  building and testing it. The [Quickstart](#quickstart) below covers getting n8n running with
  zero prior n8n experience.

## Install

Inside your own n8n instance:

```bash
npm install n8n-nodes-algenta
```

or install `n8n-nodes-algenta` from n8n's *Community Nodes* settings page, then restart n8n. To
build from source instead — for development, or to try the node in a throwaway local n8n before
connecting it to an engine — see the [Quickstart](#quickstart) below.

## Quickstart

### Try it locally (no Algenta Engine required)

This gets the real node loaded into a real, local n8n editor so you can see its operations, typed
fields, and Tool Profile selector — no self-hosted engine needed yet, since nothing here calls one:

```bash
git clone https://github.com/thyn-ai/algenta-integrations.git
cd algenta-integrations/typescript/n8n-nodes-algenta
pnpm install
pnpm --filter n8n-nodes-algenta dev
```

`pnpm dev` (via [`@n8n/node-cli`](https://www.npmjs.com/package/@n8n/node-cli)) symlinks this
package into a throwaway local n8n user folder and launches a real n8n instance with it already
installed — no `npm publish` and no manual community-node install step. Once it logs
`Editor is now accessible`, open **http://localhost:5678**, create a new workflow, and add an
**Algenta** node. Adding an **Algenta API** credential and pressing **Test** at this point will
fail with a plain connection error (nothing is listening on `http://localhost:8000`) — that is the
expected, correct result with no engine running yet, not a bug.

### Run the full example (needs a running engine)

Once you have a self-hosted Algenta Engine reachable (see [Prerequisites](#prerequisites)), import
[`demo/n8n/log-and-execute-decision.json`](../../../../demo/n8n/log-and-execute-decision.json) —
a real workflow that logs a decision and executes it, branching on whether the engine's policy
allowed or blocked the execution. See [`demo/n8n/README.md`](../../../../demo/n8n/README.md) for
the exact import steps and what each node in it does.

## Self-hosted-first

This node talks to **your own self-hosted Algenta Engine** over its MCP endpoint (`{baseUrl}/mcp`)
— never a hosted-by-Algenta cloud service. Configure `Base URL` (default
`http://localhost:8000`, no `/mcp` suffix — the node appends it) and, if your engine requires one,
an `API Key` (sent as a bearer token) on the `Algenta API` credential.

## Tool profiles

| Profile | Allows |
|---|---|
| `observe` (default) | Get Contract, Query Data, Simulate, Recommend |
| `govern` | + Plan Decision, Log Decision |
| `execute` | + **Execute Decision** |
| `full` | opt-in — every tool the connected engine's MCP registry advertises |

Matches [`contracts/integration-tool-contract.json`](../../../../contracts/integration-tool-contract.json)
exactly — see `test/contract-parity.test.ts`.

## Arguments

`Get Contract` and `Execute Decision` (`Decision ID` and `Webhook URL`) have real typed fields. The other
five operations take a JSON `Arguments` object, because their exact shape is discovered from the
connected engine's own MCP contract at call time and can vary by engine version — hardcoding typed
fields for them would mean guessing a schema no other package in this repository assumes either
(see `algenta-tools/src/toolset.ts`'s identical reasoning for the sibling Vercel AI SDK package).

## Why `@modelcontextprotocol/sdk` directly

n8n's own bundled MCP nodes use the official TypeScript SDK under the hood, and Algenta's MCP
server speaks the same protocol (Streamable HTTP, bearer auth) those nodes already talk to — so
this package uses the same SDK directly rather than depending on `algenta-tools` (which is built
around the Vercel AI SDK's `ToolSet` abstraction, a different execution model than n8n's own node
`execute()` lifecycle).

## n8n Cloud eligibility

This node is **not eligible for n8n's Cloud verification program** — that program requires zero
runtime dependencies (community nodes share one n8n instance's `node_modules`, so n8n Cloud bans
anything that could conflict), and this node genuinely needs `@modelcontextprotocol/sdk` to speak
MCP correctly rather than hand-rolling the protocol's session/handshake/SSE framing. It is a fully
real, tested community node for **self-hosted n8n** — this program's actual target audience —
installed the normal way (`npm install n8n-nodes-algenta` inside your n8n instance, or via the
*Community Nodes* settings page). `n8n-node lint` accordingly runs against
`configWithoutCloudSupport`, not the default strict/cloud config; see `eslint.config.mjs`.

Two lint findings are deliberately left as documented exceptions rather than "fixed" into
something worse:
- The `"dependencies"` field lint (`@n8n/community-nodes/no-runtime-dependencies`) flags
  `@modelcontextprotocol/sdk` — the same tradeoff as above, not an oversight.
- `import-x/no-unresolved` on the SDK's `client/*.js` and `server/*.js` subpath imports is the
  bundled eslint config's resolver not following the SDK's `exports` map in this setup; `tsc`
  resolves them correctly (see `pnpm build`) and the test suite exercises them over a real socket.

## Development

```bash
pnpm install
pnpm build
pnpm test
pnpm lint
```

Tests run a real `@modelcontextprotocol/sdk` `McpServer` over a real local HTTP socket
(`test/test-support/stub-server.ts`) and exercise this package's actual client/node code against
it — not a mock of the SDK's internals.

## License

This package's `package.json` declares `"license": "Apache-2.0"`, matching the rest of this
repository (the root [LICENSE](../../../../LICENSE), [NOTICE](../../../../NOTICE), and the
workspace root `package.json`).

n8n's own community-node verification lint
(`n8n-nodes-base/community-package-json-license-not-default`) hard-requires this field to read
exactly `MIT` on any package carrying the `n8n-community-node-package` keyword — this package
deliberately does not comply, and that rule is turned off in `eslint.config.mjs` with a comment
explaining why. This is a deliberate, informed choice: org-wide license consistency across every
package in this repository was judged more important than eligibility for n8n's official
community-node registry listing. The practical consequence is that this node likely cannot be
submitted to or discovered through that registry, but it remains a fully real, tested, installable
community node for self-hosted n8n via direct install (`npm install`, or building from source —
see "Development" above).
