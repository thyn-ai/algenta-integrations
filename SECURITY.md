# Security Policy

This repository contains framework-integration packages that wrap Algenta's
published Python and TypeScript client SDKs for use with agent frameworks
(pydantic-ai, LangChain, LiteLLM, and TypeScript/JS tool-calling helpers). We
take the security of these integrations seriously and appreciate responsible
disclosure from the community.

## Supported versions

Each package in this repository is versioned and released independently
(see the per-package tags and GitHub Releases). Security fixes land on
`main` and in the latest release of each affected package.

| Channel | Supported |
| --- | --- |
| Latest release of each package / `main` | :white_check_mark: |
| Older releases | Best-effort; please upgrade to the latest |

## Release integrity

Every GitHub Release cut after the signed-release pipeline landed (late
September 2026) carries the published artifacts, a keyless Sigstore
signature bundle per asset and SLSA build provenance, all produced by the
release workflow itself. Releases cut before that carry no assets and are
not retro-signed. How to check a release's assets with `cosign` and
`slsa-verifier` is in
[CONTRIBUTING.md → Verifying a release](./CONTRIBUTING.md#verifying-a-release).
An asset that fails verification is a security report — please send it
through the channels below.

## Reporting a vulnerability

**Please do not open a public issue, pull request, or discussion for
security problems.** Public disclosure before a fix is available puts other
users at risk.

Report privately through either channel:

1. **GitHub Security Advisories** (preferred) — open a private report from
   this repository's **Security → Report a vulnerability** tab.
2. **Email** — `security@algenta.ai`.

Please include, where possible: a description of the issue and its impact,
the affected package (e.g. `pydantic-ai-algenta`, `langchain-algenta`,
`litellm-algenta`, `algenta-tools`), steps to reproduce or a proof of
concept, and the package version you tested.

## What to expect

- Acknowledgement within 3 business days.
- An initial assessment and severity triage within 7 business days.
- Regular updates as we work on a fix, and credit in the published advisory
  (unless you prefer to remain anonymous).
- Coordinated disclosure: we agree on a timeline with you and publish a
  GitHub Security Advisory once a fix is available.

## Scope

**In scope** — this repository's own code, including:

- Every integration package's handling of credentials passed through to the
  underlying `algenta-sdk` client (e.g. accidental logging of API keys or
  bearer tokens)
- Tool-profile enforcement bugs — e.g. a framework adapter that exposes an
  `execute`-tier tool (such as `algenta_execute_decision`) under the
  `observe` or `govern` profile, or that lets a model-facing call set
  `force` / `override_safety`, which must never be model-facing per
  [`contracts/integration-tool-contract.json`](./contracts/integration-tool-contract.json)
- Insecure defaults in any package in `python/` or `typescript/`

**Out of scope for this repository** (redirect privately to
`security@algenta.ai`, same as above, rather than filing here):

- The Algenta Engine itself, its entitlement/license enforcement, or any of
  its closed-source implementation (lives in a separate private repository)
- The `algenta-sdk` client libraries' own transport/auth code (that's
  `thyn-ai/algenta-sdk`'s `SECURITY.md`, not this repository's)
- Algenta's internal control-plane and hosted infrastructure

We also want to be upfront about the trust model: every package here is a
thin wrapper around the published `algenta-sdk` client and is designed to be
**assumed untrusted**. A report showing that a package's own client-side
checks can be bypassed by modifying the package is informational, not a
vulnerability, unless it also demonstrates that the closed engine's
independent, server-side entitlement or governance enforcement was
bypassed. The engine, not any package in this repository, is the sole
authority over licensed capacity and execute-tier safety gates.

**Also out of scope:** third-party dependencies (report those upstream; we
still want to hear how they affect Algenta), and social-engineering,
physical, or denial-of-service testing against any hosted environment.

Thank you for helping keep Algenta and its users safe.
