# Algenta × Vercel AI SDK — Recipes

Ten runnable, tested recipes that put [Algenta](https://algenta.ai) at the center of the most
popular [Vercel AI SDK](https://ai-sdk.dev) (`ai` v7) patterns, built on the published
[`algenta-tools`](../packages/algenta-tools/README.md) governed `ToolSet` factory. Each recipe
is real code with a real test suite — no pseudo-code, no placeholders.

Every recipe runs **with zero credentials**: the Algenta side goes over the real MCP wire to a
local stub engine ([`src/support/stub-server.ts`](./src/support/stub-server.ts), the same
local-stub-server pattern every integration in this repository uses), and model turns run
through the AI SDK's own `MockLanguageModelV4` scripted stand-in
([`src/support/scripted-model.ts`](./src/support/scripted-model.ts)). Swap the stub's `baseUrl`
for your self-hosted Algenta Engine and the scripted model for any real provider model, and the
same code is the real thing.

## Run a recipe

```bash
cd typescript/algenta-tools
pnpm install
pnpm --filter algenta-tools-recipes recipe:01   # ...through recipe:10
```

## Run the tests

```bash
cd typescript/algenta-tools
pnpm --filter algenta-tools-recipes test
```

## Index

| # | Recipe | One-line value | Run |
|---|---|---|---|
| 01 | [Governed agent](./src/01-governed-agent.ts) | `ToolLoopAgent` whose tool surface is an Algenta profile — write/execute tools are never even offered to the model | `pnpm --filter algenta-tools-recipes recipe:01` |
| 02 | [Structured denial as typed error](./src/02-structured-denial-as-typed-error.ts) | Branch on `error.gate` (`idempotency` / `confidence` / `risk_floor`) instead of parsing messages | `pnpm --filter algenta-tools-recipes recipe:02` |
| 03 | [Receipt-typed results](./src/03-receipt-typed-results.ts) | `execute_decision` returns a typed `ExecutionReceipt`; project it into audit records safely | `pnpm --filter algenta-tools-recipes recipe:03` |
| 04 | [BM25 retrieval tool](./src/04-bm25-retrieval-tool.ts) | Agentic RAG with a real Okapi BM25 `tool`; every retrieval recorded in decision memory | `pnpm --filter algenta-tools-recipes recipe:04` |
| 05 | [Decision simulation tool](./src/05-decision-simulation-tool.ts) | One model-facing tool composing `simulate` + `recommend` + `log_decision` | `pnpm --filter algenta-tools-recipes recipe:05` |
| 06 | [Profile-filtered toolsets](./src/06-profile-filtered-toolset.ts) | Least-privilege role→profile toolsets (analyst/planner/operator/admin); schema & argument scrubbing verified | `pnpm --filter algenta-tools-recipes recipe:06` |
| 07 | [Streaming governed turns](./src/07-streaming-governed-turns.ts) | Lift typed receipts and named-gate denials out of `streamText`'s `fullStream` parts | `pnpm --filter algenta-tools-recipes recipe:07` |
| 08 | [Approval flow with receipts](./src/08-approval-flow-with-receipts.ts) | Human-in-the-loop approve/reject before execution; human approval never bypasses engine policy | `pnpm --filter algenta-tools-recipes recipe:08` |
| 09 | [generateObject decision schema](./src/09-generate-object-decision-schema.ts) | Schema-validated decision proposals recorded to decision memory — invalid output never gets logged | `pnpm --filter algenta-tools-recipes recipe:09` |
| 10 | [Audit logging middleware](./src/10-audit-logging-middleware.ts) | `wrapLanguageModel` middleware recording every call with the tool profile + offered tool names | `pnpm --filter algenta-tools-recipes recipe:10` |

## Layout

- `src/NN-*.ts` — the recipes. Each exports its functions plus a `main()` wired to the run
  commands above.
- `src/NN-*.test.ts` — the per-recipe test suites (happy path, edge cases, failure cases).
- `src/recipes-standalone.test.ts` — smoke-runs every recipe's `main()` end-to-end.
- `src/support/` — the local stand-ins (stub engine, scripted model, shared plumbing). Test/run
  infrastructure, not recipe surface.

## Notes

- Node.js 22 or later.
- The recipes consume `algenta-tools` exactly as an external user would (its published export
  surface), via the pnpm workspace — they are a compile-time check on the package's public API
  as well as examples.
- Not published to npm; clone the repository to use them (see
  [Building from source](../packages/algenta-tools/README.md#building-from-source)).
