# Contributing to Algenta Integrations

Thank you for your interest in contributing. This repository holds the
framework-integration packages that let agent frameworks — pydantic-ai,
LangChain/LangGraph, LiteLLM, Microsoft Agent Framework, Haystack,
LlamaIndex, Ray Serve, vLLM, the Vercel AI SDK, and n8n — call a
self-hosted [Algenta](https://algenta.ai) engine through its published
client SDKs and public HTTP/MCP surface.

The Algenta engine itself is closed source and lives in a separate,
private repository. The `algenta-sdk` client libraries these packages
build on are Apache-2.0 and live in the sibling repository
[`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk). Nothing
in this repository grants access to the engine — see
[The one rule that matters most](#the-one-rule-that-matters-most) below.

All ten integration packages are implemented and carry their own passing
test suites. For per-package status, the roadmap, and exactly what
"implemented" does and does not mean yet, see the root
[README](./README.md#status--roadmap).

## Repository layout

| Path | What it is |
|---|---|
| `python/` | A [uv](https://docs.astral.sh/uv/) workspace with one member per Python integration package (`pydantic-ai-algenta`, `langchain-algenta`, `litellm-algenta`, `maf-algenta`, `haystack-algenta`, `llamaindex-algenta`, `ray-serve-algenta`, `vllm-algenta`), plus `examples/` (usage examples, not a released package) |
| `typescript/` | Two independent [pnpm](https://pnpm.io/) + [Turborepo](https://turbo.build/) workspaces — `typescript/algenta-tools/` (Vercel AI SDK) and `typescript/n8n-nodes-algenta/` (n8n community node) — each holding its publishable package under `packages/` |
| `contracts/` | The shared tool-profile contract (`integration-tool-contract.json`) every package must conform to |
| `scripts/` | CI gates and release automation (standard-library Python, plus one plain-TypeScript twin) |
| `demo/` | The cross-framework conformance suite — runs against a live, self-hosted engine |
| `.github/` | CI, release, and publish workflows; issue and pull-request templates |

## Development setup

Prerequisites: Python ≥ 3.10 with [uv](https://docs.astral.sh/uv/), and
Node.js ≥ 18 (CI runs Node 20) with pnpm 9.

### Python

```bash
git clone https://github.com/thyn-ai/algenta-integrations
cd algenta-integrations/python

uv sync --all-packages --all-extras
```

`--all-extras` is required, not cosmetic: pytest and its plugins live in
each package's `dev` extra, so without them the test runner never gets
installed into the workspace environment.

Run one package's test suite — one pytest process per package, because
the suites use package-relative imports that deliberately do not collect
together in a single pytest run:

```bash
uv run pytest langchain-algenta -v
```

CI runs every package exactly this way (one process per package,
discovered by glob); `examples/` is a workspace member without a test
suite and is the one directory where collecting zero tests is expected.

### TypeScript

Each workspace under `typescript/` is self-contained, with its own
lockfile and turbo pipeline:

```bash
cd typescript/algenta-tools        # or typescript/n8n-nodes-algenta
pnpm install
pnpm turbo run build lint test
```

Run one package's tests within a workspace:

```bash
pnpm --filter algenta-tools test
```

### Linting and formatting

Python code is linted and formatted with
[Ruff](https://docs.astral.sh/ruff/) (configuration in
[`ruff.toml`](./ruff.toml)); TypeScript packages lint via `tsc --noEmit`
as part of `turbo run lint`. The repository's
[pre-commit](./.pre-commit-config.yaml) hooks run Ruff plus whitespace,
YAML/JSON, and security checks on every commit:

```bash
uv tool install pre-commit   # or: pipx install pre-commit
pre-commit install
```

## Commit messages: Conventional Commits (required)

This repository releases automatically from commit messages, so their
format is a hard requirement, not a style preference. After every merge
to `main`, [`scripts/compute_release_bumps.py`](./scripts/compute_release_bumps.py)
runs (via [`.github/workflows/auto-release.yml`](./.github/workflows/auto-release.yml)),
walks the commits that touched each package since that package's last
tag, and derives a per-package semantic-version bump:

| Commit message | Version effect |
|---|---|
| `feat: ...` | minor bump |
| `fix: ...` | patch bump |
| `BREAKING CHANGE:` in the body or footer, or `!` after the type/scope (e.g. `feat!:`) | major bump |
| Any other type — `docs`, `chore`, `test`, `ci`, `refactor`, `perf`, `style`, `build` | no bump |

The format is `type(scope): summary`, with the scope naming the package
you changed:

```
feat(langchain-algenta): add governed-query tool wrapper
fix(algenta-tools): correct tool-profile default
docs(vllm-algenta): document self-hosted base URL setup
```

The scope is for human readers and release notes; which package actually
gets bumped is determined by which paths the commit touched, so a commit
that only changes repository-level files (CI, root docs) bumps nothing.
Squash-merged pull requests keep this working as long as the final
squashed commit message follows the format — please make sure it does.

## The one rule that matters most

**Every package in this repository may depend only on the published
`algenta-sdk` package ([PyPI](https://pypi.org/project/algenta-sdk/) /
[npm](https://www.npmjs.com/package/algenta-sdk)), and may talk only to
the caller's own self-hosted Algenta engine over its public HTTP/MCP
surface at runtime.** The engine is closed source; nothing here may
vendor its source, import its internals, or take a path-, git-, or
file-based dependency on anything outside this repository, and no
package may depend on any Algenta-named artifact other than the
published `algenta-sdk`.

This is enforced mechanically, not by convention:
[`scripts/check-no-engine-dependency.py`](./scripts/check-no-engine-dependency.py)
scans every dependency manifest and every import/require statement in
the repository on every pull request, and
[`scripts/test_check_no_engine_dependency.py`](./scripts/test_check_no_engine_dependency.py)
exists to prove the gate actually catches violations. A pull request
that fails this check does not merge, no matter how small the violation
looks.

## Testing expectations

Every behavior change ships with tests, and CI runs the full suite on
every pull request — including forks, with no secrets — so green locally
must mean green in CI. Beyond ordinary unit coverage, changes to a
package's tool surface must preserve three properties that every
integration package's suite exercises:

- **Contract parity** — the tools a package exposes per profile match
  [`contracts/integration-tool-contract.json`](./contracts/integration-tool-contract.json)
  exactly: same tool names, same profile boundaries, same `observe`
  default.
- **Profile filtering** — an `execute`-tier tool never leaks into the
  `observe` or `govern` profiles, and `force` / `override_safety`-shaped
  fields are never model-facing, in any profile.
- **Receipts and denials** — `execute_decision`'s typed success receipt
  and its synchronous, named policy-gate denials (`idempotency`,
  `confidence`, `risk_floor`) keep their typed mapping onto the
  package's own success and error types.

Write tests the way the existing suites do: against a real stub server
speaking real HTTP/MCP, not mocks of the package's own internals.
End-to-end scenarios that need a live, self-hosted engine belong in the
[`demo/`](./demo/README.md) conformance suite, not in a package's unit
tests.

One more repository-wide rule: no hardcoded credentials, tokens, or
hosted-cloud endpoints anywhere. Every example and every default
resolves its endpoint from the caller's own self-hosted deployment
(`ALGENTA_BASE_URL`).

## Pull requests

- Branch from `main` as `feat/short-description`,
  `fix/short-description`, or `docs/short-description`.
- Keep the diff focused on one change; unrelated refactors go in their
  own pull request.
- Fill in the pull-request template's checklist — it restates the
  contract and self-hosted-only requirements above.
- All CI checks must pass. Review follows [`CODEOWNERS`](./CODEOWNERS):
  changes to `contracts/`, the no-engine-dependency gate, or anything
  under `.github/` always get deliberate maintainer review.

## Reporting issues and getting help

See [`SUPPORT.md`](./SUPPORT.md) for where to file what. Security
reports are never public — follow [`SECURITY.md`](./SECURITY.md).

## Recognizing contributors

This project follows the [all-contributors](https://allcontributors.org)
specification: everyone who contributes — code, docs, bug reports, reviews,
or any other [contribution type](https://allcontributors.org/docs/en/emoji-key) —
is recognized in the [README](./README.md#contributors). Maintainers add
contributors by commenting `@all-contributors please add @user for code`
(replacing `code` with the relevant contribution type) on an issue or pull
request, and the bot opens a pull request updating the contributors table.

## Licensing

This repository is licensed under [Apache-2.0](./LICENSE) (see
[`NOTICE`](./NOTICE)). Contributions are inbound = outbound: by
submitting a pull request, you license your contribution under the
project's existing Apache-2.0 license, consistent with section D.6 of
the [GitHub Terms of
Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#6-contributions-under-repository-license).
There is no Contributor License Agreement (CLA) to sign. Please only
contribute work you have the right to submit under these terms.

## Community

- Discord: https://algenta.ai/discord
- Docs: https://docs.algenta.ai
- Email: community@algenta.ai
