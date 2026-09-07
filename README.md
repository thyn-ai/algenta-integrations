<div align="center">

# Algenta Integrations

**Framework-integration packages that let agent frameworks call [Algenta](https://algenta.ai) through its published client SDKs.**

[![CI](https://github.com/thyn-ai/algenta-integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/thyn-ai/algenta-integrations/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Status: implemented, unpublished](https://img.shields.io/badge/status-implemented%2C%20unpublished-yellow.svg)](#status--roadmap)

[Docs](https://docs.algenta.ai) · [Contributing](./CONTRIBUTING.md) · [Security](./SECURITY.md) · [Tool-profile contract](./contracts/integration-tool-contract.json)

</div>

---

## What this repository is

This repository holds thin, framework-specific packages that map Algenta's
tool surface onto the conventions of popular agent frameworks:

| Package | Framework | Status |
|---|---|---|
| [`python/pydantic-ai-algenta`](./python/pydantic-ai-algenta) | pydantic-ai | Implemented |
| [`python/langchain-algenta`](./python/langchain-algenta) | LangChain / LangGraph | Implemented |
| [`python/litellm-algenta`](./python/litellm-algenta) | LiteLLM (MCP Gateway config, not a library) | Implemented (config + docs + real-proxy tests) |
| [`python/maf-algenta`](./python/maf-algenta) | Microsoft Agent Framework (standalone, self-hosted MCP) | Implemented |
| [`python/haystack-algenta`](./python/haystack-algenta) | Haystack (`haystack-ai` + `mcp-haystack`'s own `MCPToolset`) | Implemented -- Haystack slice of D6 only, see below |
| [`python/llamaindex-algenta`](./python/llamaindex-algenta) | LlamaIndex (`llama-index-core` + `llama-index-tools-mcp`) | Implemented -- LlamaIndex slice of D6 only, see below |
| [`typescript/algenta-tools`](./typescript/algenta-tools/packages/algenta-tools) | Vercel AI SDK (`ai` v7) tool integration | Implemented |
| [`typescript/n8n-nodes-algenta`](./typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta) | n8n community node (`@modelcontextprotocol/sdk` directly) | Implemented -- n8n slice of D6 only, see below |
| [`python/ray-serve-algenta`](./python/ray-serve-algenta) | Ray Serve (byte-transparent `/mcp` reverse proxy, KubeRay `RayService`) | Implemented -- Ray Serve slice of D6 only, see below |
| [`python/vllm-algenta`](./python/vllm-algenta) | vLLM / any OpenAI-client-based consumer, pointed at Algenta's own `/v1` surface | Implemented -- vLLM slice of D6 only, see below |

"Implemented" above means real code, wired to each framework's own
primitives, with a passing unit/integration test suite of its own -- see
[Status & roadmap](#status--roadmap) below for exactly what that does and
does not cover. It does **not** yet mean cross-framework
conformance-verified (that gate, D9, has never run against any package in
this repository -- only directly against the Algenta engine) or published
to a registry (none of these ten packages is on PyPI or npm yet). One
consistent claim, stated once here and not contradicted anywhere else in
this document: **real, independently-tested code; not yet cross-framework
verified; not yet published.**

`python/maf-algenta` is deliberately about **Microsoft Agent Framework only** -- a real,
standalone SDK, packaged for `pip install` once published (not yet — see
[Status & roadmap](#status--roadmap)), that needs no Azure account. It does not cover **Microsoft
Foundry** (the hosted Azure platform); see [`python/maf-algenta/foundry/`](./python/maf-algenta/foundry)
for that separate, documentation-only, explicitly-not-independently-verified deliverable, and this
track's own row in the table below for the honest split.

Every package may depend on at most one Algenta-owned thing: the already
published, Apache-2.0 **`algenta-sdk`** client
([PyPI](https://pypi.org/project/algenta-sdk/) /
[npm](https://www.npmjs.com/package/algenta-sdk), source at
[`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk)) — a thin
HTTP/gRPC client with zero engine source in it. No package may depend on any
other Algenta-named package, and no package may depend on `algenta-sdk` for
anything that would point it at Algenta's hosted cloud instead of the
caller's own self-hosted engine (`typescript/algenta-tools` talks MCP
directly via `@ai-sdk/mcp` and, for that reason, has no `algenta-sdk`
dependency at all — see that package's own README for why). **The Algenta
Engine itself is closed-source and lives in a separate, private repository.
It is never vendored, imported, or depended on here, in any form** — see
[Enforcement](#enforcement-not-just-a-policy-note) below.

## Self-hosted-only positioning

Every example, default, and piece of documentation in this repository
resolves its Algenta endpoint from **the caller's own self-hosted Algenta
Engine** — `ALGENTA_BASE_URL` pointed at your own deployment, talking to it
over HTTP or MCP (e.g. its self-hosted `/mcp` endpoint). Nothing here
defaults to, or silently falls back to, an Algenta-hosted cloud endpoint.
Where you run your Algenta Engine (your laptop, your datacenter, your cloud
account) is entirely your choice — these packages don't have an opinion
about it beyond "wherever it is, reach it over the network."

## The tool-profile model

Algenta exposes a large MCP tool registry (~117 tools across data access,
query, simulation, decisions, agent runtime, control plane, and more).
Handing all of that to a model by default is rarely what you want. Every
package in this repository maps that registry onto four **profiles**,
defined once in [`contracts/integration-tool-contract.json`](./contracts/integration-tool-contract.json)
so that "which tools does this integration expose" means the same thing in
every language and every framework:

| Profile | Adds | Notes |
|---|---|---|
| `observe` | `get_contract`, `query_data`, `simulate`, `recommend` | **Default.** Read-only situational awareness. |
| `govern` | + `plan_decision`, `log_decision` | Propose and record decisions; never executes anything. |
| `execute` | + `execute_decision` | Real-world execution. Requires explicit server-side enablement, a role/permission check, an out-of-band policy approval, an idempotency key, the `plan_hash` of an already-planned decision, and a single-use nonce — all enforced by the engine, not by any package here. `force` / `override_safety` are never model-facing, in any profile. |
| `full` | everything | Opt-in complete registry. Admin/ops tooling, never a model-facing default. |

See the contract file for the full tool list, the `execute` profile's
complete gate list, and the `algenta_`-prefixed external tool-name
convention used when a framework (e.g. Assistants-style function calling)
requires a single flat tool name instead of MCP-style namespacing.

## Enforcement, not just a policy note

`scripts/check-no-engine-dependency.py` scans every `pyproject.toml`,
`package.json`, and import/require statement in this repository on every
pull request (wired into [`.github/workflows/ci.yml`](./.github/workflows/ci.yml))
and fails the build if anything:

- depends on the private `decision-engine` repository, `mojo/`, or
  `apps/api_server` / `apps/mcp_server` (by name, by a local/relative
  filesystem path, or by import), or
- depends on "Algenta" under any name other than the published
  `algenta-sdk` package.

`scripts/test_check_no_engine_dependency.py` is the permanent, CI-enforced
proof that this check actually catches a violation (a deliberately-crafted
`package.json` depending on `"decision-engine": "file:../../decision-engine"`,
among other cases) rather than a script nobody ever exercised — see that
file for the full list of cases it proves, both caught and correctly
allowed.

## Status & roadmap

This repository started as a bootstrap scaffold (D0) and has since shipped
real, independently-tested implementations for all ten framework-integration
packages listed above (D1–D6, all marked ✅ below). Beyond those tracks,
nothing else here has real tool-calling logic yet, and none of it should be
assumed to work end-to-end.

Two things are still genuinely pending, on purpose, and neither is done yet:

- **Cross-framework conformance (D9).** The 12-scenario conformance suite in
  [`demo/`](./demo) has only ever run directly against the Algenta engine —
  never through any of these packages' own framework adapters. Until
  per-framework adapters exist and pass it, **no package in this repository
  should be called a "validated integration"** — see
  [`demo/README.md`](./demo/README.md) for the full, falsifiable accounting
  of what does and doesn't run today.
- **Publication.** None of these ten packages is on PyPI or npm yet; every
  one 404s on both registries today. See
  [Explicitly deferred](#explicitly-deferred-not-gaps--deliberate-scope-boundaries)
  below for the two owner actions that unblock it.

D7–D8 remain planned; nothing exists for them yet.

| Track | Scope | Status |
|---|---|---|
| D0 | This repository's scaffold: governance files, CI, the no-engine-dependency gate, the tool-profile contract, workspace layout | ✅ Done |
| D1 | `pydantic-ai-algenta` real implementation: a governed-execution-aware `WrapperToolset` over pydantic-ai's own `MCPToolset` — tool-profile filtering, never-model-facing field scrubbing, a typed `execute_decision` success receipt, and a `ToolDenied`-based mapping of its three real, synchronous named policy gates (`idempotency`/`confidence`/`risk_floor`) | ✅ Done |
| D2 | `typescript/algenta-tools` real implementation: an Algenta-aware Vercel AI SDK (`ai` v7) `ToolSet` — tool-profile filtering, never-model-facing field scrubbing, and `execute_decision`'s real typed success/denial contract (a typed `ExecutionReceipt` on success, a typed `ExecutionBlockedError` naming the real `idempotency`/`confidence`/`risk_floor` gate on a synchronous denial — no approval-pause state exists on the real tool, so none is modeled) | ✅ Done |
| D3 | `langchain-algenta` real implementation: a governed-execution-aware LangChain/LangGraph tool list — tool-profile filtering, never-model-facing field scrubbing, a typed `execute_decision` success receipt, and an `AlgentaExecutionBlocked`-based mapping of its three real, synchronous named policy gates (`idempotency`/`confidence`/`risk_floor`) | ✅ Done |
| D4 | `litellm-algenta` real implementation -- LiteLLM's MCP Gateway is a proxy/gateway process configured by YAML, not a library to wrap, so "real implementation" here means: a config generator/linter mapping the shared profile contract onto LiteLLM's real, verified `allowed_tools`/`allowed_params` enforcement, ready-to-use per-profile config templates, and a conformance suite that runs a real `litellm` proxy process against a real stub MCP server (never mocked) | ✅ Done (Lane 1 -- config/gateway integration; Lane 2, an upstreamed `CustomLLM` provider PR to the litellm OSS repo itself, is out of scope for this repository) |
| D5 | Microsoft Agent Framework **and** Microsoft Foundry, two different deliverables under one track — Lane 1 (`python/maf-algenta`): a real, tested, governed-execution-aware `create_algenta_tools` wrapping MAF's own `MCPStreamableHTTPTool` and `MiddlewareFailure` primitives, mapping `execute_decision`'s real synchronous success receipt / three-named-gate denial contract onto `AlgentaToolDenied`, built and verified to the same bar as D1–D4. Lane 2 (`python/maf-algenta/foundry/`): Entra app-registration Bicep template + `azd ai connection create`/Toolbox artifacts for registering Algenta's self-hosted MCP endpoint with a live Foundry project — accurate, schema-checked, and cited against current Microsoft Learn docs, but **explicitly not independently verified against a live Foundry project** (none is available in this environment) and never claimed as such. | ✅ Lane 1 done · 📋 Lane 2 out of scope for independent verification (owner-applied) |
| D6 | Five separate deliverables under one label: n8n, Haystack, LlamaIndex, Ray Serve, and vLLM. **Haystack** (`python/haystack-algenta`): a real, tested, governed-execution-aware `create_algenta_tools` wrapping Haystack's own `MCPToolset` (tool-profile filtering via its native `tool_names=`, two-layer never-model-facing scrubbing via a rebuilt `Tool`) plus `build_algenta_governance_hooks`, an `Agent` `after_tool` hook that raises a typed `AlgentaToolDenied` (naming the real `"idempotency"`/`"confidence"`/`"risk_floor"` gate) for `execute_decision`'s real, synchronous 409 denial, via Haystack's real `after_tool` hook seam. **LlamaIndex** (`python/llamaindex-algenta`): a real, tested, governed-execution-aware `create_algenta_tools` wrapping `llama-index-tools-mcp`'s own `BasicMCPClient`/`FunctionTool` primitives, with tool-profile filtering, two-layer `force`/`override_safety` scrubbing, and a typed `AlgentaToolDenied`/`AlgentaToolExecutionFailed` mapping of `execute_decision`'s real synchronous success-receipt/three-named-gate-denial contract. **n8n** (`typescript/n8n-nodes-algenta`): a real, tested community node exposing all 7 MCP tools as typed operations directly on the official `@modelcontextprotocol/sdk` (Streamable HTTP), with tool-profile enforcement checked before any network call, `execute_decision`'s real typed `ExecutionReceipt`/named-gate-denial contract mapped onto `NodeApiError`, and a real, importable production workflow (`demo/n8n/log-and-execute-decision.json`) branching on policy denial vs. success. Not eligible for n8n's own Cloud verification program (that program bans all runtime dependencies; this node genuinely needs the MCP SDK) — a real, documented tradeoff, not an oversight; see the package README. **Ray Serve** (`python/ray-serve-algenta`): a real, tested `AlgentaMCPProxy` — a byte-transparent Ray Serve reverse proxy for Algenta's stateless `/mcp` surface (verified directly against `apps/mcp_server/README.md` in `thyn-ai/algenta`: "the canonical `/mcp` route implements stateless Streamable HTTP"), run with a real 2-replica `serve.run(...)` in its own conformance suite and proven, over real HTTP with a real MCP client, to route interleaved concurrent calls across more than one live replica with zero cross-replica state leakage; ships a KubeRay `RayService` manifest (`manifests/rayservice.yaml`) with `autoscaling_config` floored at 2 replicas. It never parses MCP JSON-RPC content or reasons about tool names — profile enforcement stays entirely the connected engine's job, exactly as for any other Algenta MCP traffic. **vLLM** (`python/vllm-algenta`): a thin `build_client`/`build_async_client` helper pointing the standard, unmodified `openai` Python client at Algenta's own `/v1/chat/completions` / `/v1/responses` surface, plus a real conformance suite against a stub built field-for-field from the real engine schema (`apps/api_server/schemas/llm.py`) — and an explicit, model-dependent capability-status table: tool/function calling and a widened `finish_reason` (`"stop" | "tool_calls" | "length" | "content_filter"`) are real for a configured provider-backed model or the bundled `algenta_local` backend, proven with a genuine `tools=` round trip through the real `openai` client, not just documented; this package's own zero-config default model (`text.tokenizer`) has neither, and now REJECTS a `tools=` argument with a loud `422` instead of silently dropping it. `stream=true` on that default model is still post-hoc rechunking of an already-complete reply, not real incremental backend generation, and a completion that actually calls a tool cannot yet be streamed at all. Neither package claims more than what its own tests exercise. | ✅ Haystack done · ✅ LlamaIndex done · ✅ n8n done (self-hosted only, not Cloud-verified) · ✅ Ray Serve done · ✅ vLLM done (tool-calling and finish_reason verified real and model-dependent; capability table re-verified against current engine `main`) |
| D9 | `demo/` — the 12-scenario conformance fixture set exercising every tool profile | 📋 Planned |
| D7–D8 | Additional integration surfaces reserved in the approved plan | 📋 Planned — exact scope tracked in the approved plan, not restated here |

Do not treat any package's presence in this repository as evidence it does
anything yet — check the table above and each package's own README.

### Dependency automation

- [`.github/dependabot.yml`](./.github/dependabot.yml) checks **daily** for
  new `algenta-sdk` releases (PyPI and npm) and weekly for everything else.
- [`.github/workflows/sync-on-sdk-release.yml`](./.github/workflows/sync-on-sdk-release.yml)
  bumps every package's `algenta-sdk` pin, refreshes lockfiles, runs the
  full check suite, and opens a (not auto-merged) pull request whenever a
  new `algenta-sdk` version is detected — either via a nightly check
  against PyPI/npm (works today) or, once a companion change lands in
  `thyn-ai/algenta-sdk`'s own release workflow, immediately via a
  `repository_dispatch` event. See that workflow's header comment for the
  exact payload shape and what the sending side still needs to add.

### Explicitly deferred (not gaps — deliberate scope boundaries)

- **No real framework integration beyond D1-D6.**
  pydantic-ai, LangChain, LiteLLM, the TypeScript tool-calling helpers, MAF,
  Haystack, LlamaIndex, n8n, Ray Serve, and vLLM (D1-D6, all five D6 lanes now
  done) have real implementations -- see the Status & Roadmap table above for
  exactly what each one is. Whatever D7-D8 turn out to cover remains planned.
- **Not on PyPI or npm yet.** Every package here 404s on both registries
  today. `pip install langchain-algenta` does not work, and no claim in this
  repository should imply otherwise.

  What exists: `.github/workflows/auto-release.yml` computes per-package
  semantic-version bumps from Conventional Commits, pushes them to main, and
  tags a GitHub Release per bumped package (zero PR — see
  `scripts/compute_release_bumps.py`). `.github/workflows/publish.yml` then
  builds the released package, verifies the artifacts carry the tagged version,
  and runs `twine check --strict` / `npm pack` — **on every release, right now**,
  so the pipeline is exercised before it is ever trusted with a real upload.

  Two owner-only actions gate an actual registry upload:

  1. Register a Trusted Publisher per package — PyPI
     [pending publishers](https://pypi.org/manage/account/publishing/) (owner
     `thyn-ai`, repo `algenta-integrations`, workflow `publish.yml`, environment
     `pypi`); npm package settings → Trusted Publisher → GitHub Actions. This
     step lives entirely in the PyPI/npm account settings, so its status can't
     be verified from inside this repository or its CI.
  2. Set the repository variable `PUBLISH_TO_REGISTRIES` to `true`. **Done**
     as of this writing — but no `publish.yml` run has executed against a
     live registry since the flag flipped, so this alone has not yet put
     anything on PyPI or npm. The next tagged release is what actually
     exercises it end-to-end.

  Until a publish run against a live registry actually succeeds, treat every
  package as unpublished regardless of what either flag says. There are
  deliberately **no** `PYPI_TOKEN`/`NPM_TOKEN` secrets: publishing uses
  short-lived OIDC tokens, so there is nothing to rotate or leak.
- **No repository visibility change.** This repository stays private until
  its owner decides otherwise.

## Governance

This repository mirrors the governance model already established (and
already public) in the sibling repository
[`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk), rather than
inventing a different one:

- **License:** [Apache-2.0](./LICENSE) (see [NOTICE](./NOTICE)).
- **CLA:** [`CLA.md`](./CLA.md) — currently a draft placeholder pending
  counsel review, identical in shape to `algenta-sdk`'s own placeholder.
  The two should end up as the same counsel-approved text, signed once via
  the same CLA-assistant bot mechanism (see
  [`.github/workflows/cla.yml`](./.github/workflows/cla.yml)).
- **Code of Conduct:** [Contributor Covenant 2.1](./CODE_OF_CONDUCT.md).
- **Security:** see [`SECURITY.md`](./SECURITY.md) for scope and reporting.
- **Contributing:** see [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

Apache-2.0 — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE). The Algenta
Engine is separate software under a separate, closed license and is not
contained in this repository.
