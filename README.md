<div align="center">

# Algenta Integrations

**Official framework integrations for the [Algenta](https://algenta.ai) decision engine — governed tool profiles, typed execution receipts, and policy-gated execution for the agent frameworks you already use.**

[![CI](https://github.com/thyn-ai/algenta-integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/thyn-ai/algenta-integrations/actions/workflows/ci.yml)
[![CodeQL](https://github.com/thyn-ai/algenta-integrations/actions/workflows/codeql.yml/badge.svg)](https://github.com/thyn-ai/algenta-integrations/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/thyn-ai/algenta-integrations/badge)](https://scorecard.dev/viewer/?uri=github.com/thyn-ai/algenta-integrations)
[![codecov](https://codecov.io/gh/thyn-ai/algenta-integrations/graph/badge.svg)](https://codecov.io/gh/thyn-ai/algenta-integrations)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![All Contributors](https://img.shields.io/badge/all_contributors-0-orange.svg)](#contributors)

[Docs](https://docs.algenta.ai) · [Contributing](./CONTRIBUTING.md) · [Support](./SUPPORT.md) · [Security](./SECURITY.md) · [Tool-profile contract](./contracts/integration-tool-contract.json)

</div>

---

Algenta exposes its decision engine capabilities — querying governed data, running
simulations, planning and recording decisions, and executing them in the real world —
as a governed tool surface over MCP. Each package in this repository maps that surface
onto the conventions of one agent framework: four **tool profiles** (`observe`,
`govern`, `execute`, `full`) decide which tools a model can even see, a successful
`execute_decision` returns a typed **execution receipt**, and a call the engine's
policy refuses is denied **synchronously, by a named gate** — approvals are enforced
by the engine server-side, never delegated to the model. Everything runs against
**your own self-hosted Algenta engine**, over its public HTTP/MCP APIs.

## Packages

All ten packages are published — eight on PyPI, two on npm — and each carries its own
passing test suite that runs against a real local stub server, never a mock of the
framework's internals.

| Package | Framework | Install | What it gives you |
|---|---|---|---|
| [`pydantic-ai-algenta`](./python/pydantic-ai-algenta) | pydantic-ai | `pip install pydantic-ai-algenta` | A governed `WrapperToolset` over pydantic-ai's own `MCPToolset`; denials surface through the native `ToolDenied` primitive. |
| [`langchain-algenta`](./python/langchain-algenta) | LangChain / LangGraph | `pip install langchain-algenta` | A governed tool list built on `langchain-mcp-adapters`, with typed receipts and a catchable `AlgentaExecutionBlocked` denial. |
| [`litellm-algenta`](./python/litellm-algenta) | LiteLLM (MCP Gateway) | `pip install litellm-algenta` | A config generator, linter, and per-profile YAML templates mapping the contract onto LiteLLM's real `allowed_tools` / `allowed_params` enforcement. |
| [`maf-algenta`](./python/maf-algenta) | Microsoft Agent Framework | `pip install maf-algenta` | A governed `FunctionTool` list with two-layer safety-field scrubbing and typed denials built on MAF's `MiddlewareFailure`. |
| [`haystack-algenta`](./python/haystack-algenta) | Haystack | `pip install haystack-algenta` | A governed toolset on Haystack's own `MCPToolset`, plus an `Agent` `after_tool` governance hook for typed denials. |
| [`llamaindex-algenta`](./python/llamaindex-algenta) | LlamaIndex | `pip install llamaindex-algenta` | Governed `FunctionTool`s with typed `ExecutionReceipt` outputs and named-gate denials. |
| [`ray-serve-algenta`](./python/ray-serve-algenta) | Ray Serve / KubeRay | `pip install ray-serve-algenta` | A byte-transparent, multi-replica reverse proxy for the engine's `/mcp` surface, with a ready-to-adapt `RayService` manifest. |
| [`vllm-algenta`](./python/vllm-algenta) | vLLM / OpenAI-compatible clients | `pip install vllm-algenta` | Thin helpers pointing the standard `openai` client at the engine's `/v1/chat/completions` and `/v1/responses` API. |
| [`algenta-tools`](./typescript/algenta-tools/packages/algenta-tools) | Vercel AI SDK (`ai`) | `npm install algenta-tools` | A governed `ToolSet` factory with typed `ExecutionReceipt` results and `ExecutionBlockedError` denials. |
| [`n8n-nodes-algenta`](./typescript/n8n-nodes-algenta/packages/n8n-nodes-algenta) | n8n | `npm install n8n-nodes-algenta` | A community node exposing the governed tools as typed n8n operations, with profile enforcement before any network call. |

## Quickstart

The example below uses `langchain-algenta`; every other package follows the same
shape, and its README has the equivalent walkthrough. You need a running self-hosted
Algenta engine (reachable at `ALGENTA_BASE_URL`) and a model-provider key
(`OPENAI_API_KEY` here). No engine yet? Each package's README has a "Try it locally"
section that runs against a local stub server with neither.

```bash
pip install "langchain-algenta[quickstart]"
```

```python
import asyncio

from langchain.agents import create_agent
from langchain_algenta import create_algenta_tools

async def main() -> None:
    tools = await create_algenta_tools(base_url="http://localhost:8000/mcp", profile="observe")
    agent = create_agent("openai:gpt-5", tools=tools)
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What's the expected value of scenario X?"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

`profile="observe"` is the default and read-only: the agent can call `get_contract`,
`query_data`, `simulate`, and `recommend` — nothing that plans, logs, or executes
anything. Opt into more with the tool profiles below.

## The tool-profile contract

[`contracts/integration-tool-contract.json`](./contracts/integration-tool-contract.json)
is the single source of truth for which of the engine's MCP tools belong to which
profile, which profile is the default, and which `execute_decision` fields (`force`,
`override_safety`) may never be model-facing, in any profile. Every package carries a
contract-parity test that loads this file and asserts its exposed tool sets match it
exactly, so "which tools does this integration expose" means the same thing in every
language and framework. It also defines the `algenta_`-prefixed naming convention
integrations must use when a framework requires one flat tool name instead of
MCP-style namespacing.

| Profile | Adds | Notes |
|---|---|---|
| `observe` | `get_contract`, `query_data`, `simulate`, `recommend` | **Default.** Read-only situational awareness. |
| `govern` | + `plan_decision`, `log_decision` | Propose and record decisions; never executes anything. |
| `execute` | + `execute_decision` | Real-world execution, gated server-side by the engine: explicit operator enablement, a role/permission check, and a `decision_id` from an already-logged decision. A call either returns a typed `ExecutionReceipt` or is denied in the same call, naming one of three policy gates — `idempotency`, `confidence`, `risk_floor`. `force` / `override_safety` are operator-only and never model-facing, in any profile. |
| `full` | everything the connected engine advertises | Opt-in complete registry. Admin/ops tooling, never a model-facing default. |

## Conformance demo

[`demo/`](./demo) holds a twelve-scenario conformance suite — one fixture set, one
runner — covering the governed-execution lifecycle end to end: read-only
recommendation, policy denial by named gate, approval granted and rejected,
duplicate-execution prevention, execution-receipt verification, and audit-export hash
checks. Today it runs directly against the engine over HTTP as the shared baseline;
per-framework adapters that drive the same scenarios through each package are the next
milestone, and until they pass, no package here claims the "validated integration"
label. See [`demo/README.md`](./demo/README.md) for the exact, falsifiable accounting
of what runs today — including the scenarios that are blocked and why.

## Self-hosted by default — no hosted fallback

Every example, default, and piece of documentation in this repository resolves its
Algenta endpoint from **your own self-hosted engine** — `ALGENTA_BASE_URL` pointed at
your own deployment, over HTTP or MCP. Nothing here defaults to, or silently falls
back to, an Algenta-hosted cloud endpoint.

This is a deliberate difference from the
[`algenta-sdk`](https://github.com/thyn-ai/algenta-sdk) client these packages build
on, whose `AlgentaClient` defaults to the hosted `https://api.algenta.ai` for a
zero-config quickstart. These integrations never use that default: the endpoint
resolves from `base_url=` / `ALGENTA_BASE_URL` (with a localhost fallback for a
local engine), full stop. Where you run the engine — your laptop, your datacenter,
your cloud account — is entirely your choice; these packages only require that it
is reachable over the network.

## Powered by Mojo

The Algenta engine's compute kernels are proprietary, written in
[Mojo](https://www.modular.com/mojo), and distributed with the engine as signed
wheels. None of that code is in this repository: these integrations — like the
[`algenta-sdk`](https://github.com/thyn-ai/algenta-sdk) client libraries they build
on — are ordinary Python and TypeScript, fully open under Apache-2.0, and talk to the
engine only through its public HTTP/MCP APIs.

## Enforcement, not just a policy note

The Algenta engine is closed source and lives in a separate, private repository. It is
never vendored, imported, or depended on here, in any form — and that boundary is
checked mechanically, not by convention. `scripts/check-no-engine-dependency.py`
scans every `pyproject.toml`, `package.json`, and import/require statement in this
repository on every pull request (wired into
[`.github/workflows/ci.yml`](./.github/workflows/ci.yml)) and fails the build if
anything depends on the engine — by name, by internal source-layout path, by a
local/relative filesystem path, or by import — or on any Algenta-named package other
than the published `algenta-sdk`.

`scripts/test_check_no_engine_dependency.py` is the permanent, CI-enforced proof that
this check actually catches a violation (a deliberately crafted `package.json`
depending on the engine repository by a relative path, among other cases) rather than
passing as a script nobody ever exercised — see that file for the full list of cases
it proves, both caught and correctly allowed.

## Status & roadmap

Every package above is published on PyPI or npm and covered by its own real test
suite. Two things are still honestly pending:

- **Per-framework conformance adapters** — see [Conformance demo](#conformance-demo).
- **A small number of engine capabilities whose verification needs a live deployment
  topology** (for example, a fault-injection proxy for real timeout behavior). Where
  something is unverified, the docs say so rather than implying otherwise.

## Contributing, governance, and support

- [Contributing](./CONTRIBUTING.md) — development setup, testing, the release
  process, and [how to verify a release](./CONTRIBUTING.md#verifying-a-release).
  Contributions are inbound=outbound under GitHub's Terms of Service §D.6;
  there is no CLA.
- [Governance](./GOVERNANCE.md) — how decisions about this repository get made.
- [Support](./SUPPORT.md) — where to ask questions and what is covered.
- [Security](./SECURITY.md) — scope and how to report a vulnerability.
- [Code of Conduct](./CODE_OF_CONDUCT.md) — Contributor Covenant 2.1.
- SDK: [`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk) — the published
  Python and TypeScript client for the engine's public API.

## Community

- [Discord](https://discord.gg/w8NDsph9an)
- community@algenta.ai

## Contributors

Thanks to everyone who contributes to this project — we follow the
[all-contributors](https://allcontributors.org) specification and recognize
contributions of [every kind](https://allcontributors.org/docs/en/emoji-key),
not just code.

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->

<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->
<!-- ALL-CONTRIBUTORS-LIST:END -->


## Related repositories

Open-source repositories from the Algenta team. The Algenta engine itself is proprietary; everything listed here is Apache-2.0. Issues and discussions are welcome in whichever repository owns the code.

- [thyn-ai/algenta-sdk](https://github.com/thyn-ai/algenta-sdk) — Python & TypeScript SDKs for the Algenta decision engine: governed tool profiles, execution receipts, approvals.
- [thyn-ai/algenta-integrations](https://github.com/thyn-ai/algenta-integrations) (this repository) — Framework integrations for Algenta: LangChain, LlamaIndex, pydantic-ai, MAF, Haystack, LiteLLM, Ray Serve, vLLM, Vercel AI SDK and n8n.
- [thyn-ai/mojo-kernels](https://github.com/thyn-ai/mojo-kernels) — Clean-room Mojo kernels as drop-in accelerators for popular Python/TypeScript libraries, with bit-exact parity and pure-language fallbacks.
- [thyn-ai/security-toolchain](https://github.com/thyn-ai/security-toolchain) — The pinned, checksum-verified security toolchain (Gitleaks, Opengrep, OSV-Scanner, Trivy config, actionlint) that every thyn-ai repository runs locally and in CI.
- [thyn-ai/feedback](https://github.com/thyn-ai/feedback) — Public issue intake for the Algenta family of open-source projects and the Codna GitHub App.
- [thyn-ai/codna-action](https://github.com/thyn-ai/codna-action) — Public GitHub Action wrapper for Codna.

## License

Apache-2.0 — see [LICENSE](./LICENSE) and [NOTICE](./NOTICE). The Algenta engine is
separate software under a separate, closed license and is not contained in this
repository.
