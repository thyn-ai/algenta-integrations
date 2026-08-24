<div align="center">

# Algenta Integrations

**Framework-integration packages that let agent frameworks call [Algenta](https://algenta.ai) through its published client SDKs.**

[![CI](https://github.com/thyn-ai/algenta-integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/thyn-ai/algenta-integrations/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Status: bootstrap](https://img.shields.io/badge/status-bootstrap%20(D0)-orange.svg)](#status--roadmap)

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

`python/maf-algenta` is deliberately about **Microsoft Agent Framework only** -- a real,
pip-installable, standalone SDK that needs no Azure account. It does not cover **Microsoft
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

This repository started as a **bootstrap scaffold (D0)**. Beyond the tracks
marked ✅ below, nothing else here has real tool-calling logic yet, and none
of those should be assumed to work end-to-end.

| Track | Scope | Status |
|---|---|---|
| D0 | This repository's scaffold: governance files, CI, the no-engine-dependency gate, the tool-profile contract, workspace layout | ✅ Done |
| D1 | `pydantic-ai-algenta` real implementation: a governed-execution-aware `WrapperToolset` over pydantic-ai's own `MCPToolset` — tool-profile filtering, never-model-facing field scrubbing, a typed `execute_decision` success receipt, and a `ToolDenied`-based mapping of its three real, synchronous named policy gates (`idempotency`/`confidence`/`risk_floor`) | ✅ Done |
| D2 | `typescript/algenta-tools` real implementation: a governed-execution-aware Vercel AI SDK (`ai` v7) `ToolSet` — tool-profile filtering, never-model-facing field scrubbing, typed governed-execution receipts, and a `needsApproval`-based approval-flow mapping | ✅ Done |
| D3 | `langchain-algenta` real implementation: a governed-execution-aware LangChain/LangGraph tool list — tool-profile filtering, never-model-facing field scrubbing, typed governed-execution receipts, and a native `langgraph.types.interrupt()`-based approval-flow mapping (with an honest fallback accounting for when no checkpointer is present) | ✅ Done |
| D4 | `litellm-algenta` real implementation -- LiteLLM's MCP Gateway is a proxy/gateway process configured by YAML, not a library to wrap, so "real implementation" here means: a config generator/linter mapping the shared profile contract onto LiteLLM's real, verified `allowed_tools`/`allowed_params` enforcement, ready-to-use per-profile config templates, and a conformance suite that runs a real `litellm` proxy process against a real stub MCP server (never mocked) | ✅ Done (Lane 1 -- config/gateway integration; Lane 2, an upstreamed `CustomLLM` provider PR to the litellm OSS repo itself, is out of scope for this repository) |
| D5 | Microsoft Agent Framework **and** Microsoft Foundry, two different deliverables under one track — Lane 1 (`python/maf-algenta`): a real, tested, governed-execution-aware `create_algenta_tools` wrapping MAF's own `MCPStreamableHTTPTool`, `approval_mode`, and `MiddlewareFailure` primitives, built and verified to the same bar as D1–D4. Lane 2 (`python/maf-algenta/foundry/`): Entra app-registration Bicep template + `azd ai connection create`/Toolbox artifacts for registering Algenta's self-hosted MCP endpoint with a live Foundry project — accurate, schema-checked, and cited against current Microsoft Learn docs, but **explicitly not independently verified against a live Foundry project** (none is available in this environment) and never claimed as such. | ✅ Lane 1 done · 📋 Lane 2 out of scope for independent verification (owner-applied) |
| D6 | Five separate deliverables under one label: n8n, Haystack, LlamaIndex, Ray Serve, and vLLM. **Haystack** (`python/haystack-algenta`): a real, tested, governed-execution-aware `create_algenta_tools` wrapping Haystack's own `MCPToolset` (tool-profile filtering via its native `tool_names=`, two-layer never-model-facing scrubbing via a rebuilt `Tool`) plus `build_algenta_governance_hooks`, an `Agent` `after_tool` hook that raises a typed `AlgentaToolDenied` (naming the real `"idempotency"`/`"confidence"`/`"risk_floor"` gate) for `execute_decision`'s real, synchronous 409 denial, via Haystack's real `after_tool` hook seam. **LlamaIndex** (`python/llamaindex-algenta`): a real, tested, governed-execution-aware `create_algenta_tools` wrapping `llama-index-tools-mcp`'s own `BasicMCPClient`/`FunctionTool` primitives, with tool-profile filtering, two-layer `force`/`override_safety` scrubbing, and a real approval mapping onto `llama-index-workflows`' `Context.wait_for_event()` human-in-the-loop primitive. Both built and verified to the same bar as D1–D5. n8n, Ray Serve, and vLLM are **not started** — do not assume any of the three has real tool-calling logic because this row exists. | ✅ Haystack done · ✅ LlamaIndex done · 📋 n8n / Ray Serve / vLLM not started |
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

- **No real framework integration beyond D1-D6's Haystack/LlamaIndex lanes.**
  pydantic-ai, LangChain, LiteLLM, the TypeScript tool-calling helpers, MAF,
  Haystack, and LlamaIndex (D1-D6) now have real implementations -- see the
  Status & Roadmap table above for exactly what each one is. `demo/`'s
  conformance fixture set (D9), D6's remaining n8n/Ray Serve/vLLM lanes, and
  whatever D7-D8 turn out to cover remain planned.
- **No publishing.** `.github/workflows/auto-release.yml` computes per-package
  semantic-version bumps from Conventional Commits and pushes them straight to
  main, tagging a GitHub Release per bumped package (zero PR — see
  `scripts/compute_release_bumps.py`), but there is no PyPI or npm publish step
  anywhere in this repository, no `PYPI_TOKEN`/`NPM_TOKEN`, and no OIDC Trusted
  Publisher registration. This repository is private; publishing is a separate,
  explicit, owner-gated decision for later.
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
