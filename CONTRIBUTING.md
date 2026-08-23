# Contributing to Algenta Integrations

Thank you for your interest in contributing. This repository holds
**framework-integration packages** — thin adapters that let agent
frameworks (pydantic-ai, LangChain, LiteLLM, and TypeScript/JS tool-calling
frameworks) call Algenta through the published `algenta-sdk` client.

The Algenta Engine itself (the compute/decision runtime these integrations
ultimately talk to, over HTTP/MCP, against a customer's own self-hosted
deployment) is closed and lives in a separate, private repository. The
`algenta-sdk` client libraries these packages depend on are Apache-2.0 and
live in the sibling repository
[`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk). Nothing in
this repository grants access to the engine, and no package here may vendor
engine source, import engine-internal modules, or depend on anything other
than the published `algenta-sdk` package (see
[`scripts/check-no-engine-dependency.py`](./scripts/check-no-engine-dependency.py),
which enforces this on every pull request — not just as a policy note).

## Status

This repository is a scaffold. No framework integration is implemented yet
— see the root [README.md](./README.md#status--roadmap) for what's planned
(D1–D9) versus what exists today.

## What you can contribute

| Area | Status | Notes |
|------|--------|-------|
| `python/pydantic-ai-algenta/` | 🚧 Scaffolded, implementation pending | |
| `python/langchain-algenta/` | 🚧 Scaffolded, implementation pending | |
| `python/litellm-algenta/` | 🚧 Scaffolded, implementation pending | |
| `typescript/algenta-tools/` | 🚧 Scaffolded, implementation pending | |
| `contracts/integration-tool-contract.json` | ✅ Open | The shared tool-profile contract every package must conform to |
| `scripts/check-parity.{py,ts}` | ✅ Open | Cross-language parity checks (stubs today) |
| `demo/` | 🔒 Not yet — see `demo/README.md` | The 12-scenario conformance fixture set is future work (tracked as D9) |

## Getting started

```bash
git clone https://github.com/thyn-ai/algenta-integrations
cd algenta-integrations

# Python workspace (uv)
cd python
uv sync
uv run pytest

# TypeScript workspace (pnpm + turbo)
cd typescript/algenta-tools
pnpm install
pnpm turbo run build test
```

Every package here is a plain client of Algenta's published SDK — you do
not need a running Algenta Engine to work on most of this code. Anything
that requires a live self-hosted endpoint to exercise end-to-end says so in
its own `README.md`.

## The one rule that matters most

**Every package may depend only on the published `algenta-sdk` (PyPI) /
`algenta-sdk` (npm) client, plus the customer's own self-hosted Algenta
Engine over HTTP/MCP at runtime.** Never:

- a relative/local path into `decision-engine`, `mojo/`, or
  `apps/api_server/`
- a git/file/path dependency of any kind
- a bundled or embedded engine of any form

CI runs [`scripts/check-no-engine-dependency.py`](./scripts/check-no-engine-dependency.py)
on every PR to enforce this mechanically. A PR that fails this check will
not merge, no matter how small the violation looks.

## Development workflow

### Branch naming
- `feat/short-description` — new feature
- `fix/short-description` — bug fix
- `docs/short-description` — documentation only

### Commit messages
We follow [Conventional Commits](https://www.conventionalcommits.org/),
which also drives this repository's automated version bumps (see
`.github/workflows/auto-release.yml`):
```
feat(langchain-algenta): add governed-query tool wrapper
fix(algenta-tools): correct tool-profile default
docs(pydantic-ai-algenta): document self-hosted base URL setup
```

### Pull request checklist
- [ ] Tests pass locally
- [ ] New features have tests
- [ ] Documentation updated if needed
- [ ] No hardcoded credentials, secrets, or base URLs pointing at a hosted
      cloud endpoint (every example defaults to the customer's own
      self-hosted `ALGENTA_BASE_URL`)
- [ ] `scripts/check-no-engine-dependency.py` passes
- [ ] Any new tool exposed to a model is placed in the correct profile per
      [`contracts/integration-tool-contract.json`](./contracts/integration-tool-contract.json)
      (`execute`-tier tools are never in `observe`/`govern`, and
      `force`/`override_safety`-shaped fields are never model-facing)
- [ ] CLA signed (the CLA-assistant bot will comment on your first PR with
      instructions)

All CI checks must pass, including on forked-repository pull requests — CI
runs with no secrets and no elevated permissions, so it's safe to run
automatically on every PR.

## Contributor License Agreement

By submitting a pull request, you'll be asked to sign Algenta's Contributor
License Agreement (a perpetual, worldwide, irrevocable grant letting
Algenta, Inc. use your contribution across this and other Algenta
products). The CLA-assistant bot handles this automatically on your first
PR — you only need to sign once. See [`CLA.md`](./CLA.md) for the full text
(currently a draft placeholder pending counsel review, mirroring
`thyn-ai/algenta-sdk`'s own placeholder — see that file for why).

## Reporting issues

- **Security vulnerabilities** → see [SECURITY.md](./SECURITY.md) (do NOT
  open a public issue)
- **Bugs** → [GitHub Issues](https://github.com/thyn-ai/algenta-integrations/issues)
  with the `bug` label
- **Questions** → GitHub Issues with the `question` label, or
  https://algenta.ai/discord

## Community

- Discord: https://algenta.ai/discord
- Docs: https://docs.algenta.ai
- Email: community@algenta.ai
