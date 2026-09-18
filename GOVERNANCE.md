# Governance

This document describes how the `thyn-ai/algenta-integrations` project
makes decisions. It mirrors the governance model of the sibling
[`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk)
repository, so contributing across both works the same way.

## Roles

### Contributors

Anyone who opens an issue, files a pull request, or reviews someone
else's work. There is no bar to entry — see
[`CONTRIBUTING.md`](./CONTRIBUTING.md) for how to start.

### Maintainers

Contributors with write access, listed in [`CODEOWNERS`](./CODEOWNERS).
Maintainers review and merge pull requests, triage issues, and carry
special responsibility for the two things this repository exists to keep
true: no package ever depends on the closed Algenta engine (only on the
published `algenta-sdk` and the engine's public HTTP/MCP surface), and
every package conforms to the shared tool-profile contract in
[`contracts/`](./contracts/integration-tool-contract.json).

### Organization owner

The owner of the `thyn-ai` GitHub organization holds final authority
over this repository: settings and visibility, release credentials and
registry publishing, maintainer appointments and removals, changes to
this document, and any contested decision. Today the organization has a
single member, so the maintainer and owner roles are held by the same
person; this document is written for the team the project intends to
grow into.

## Decision making

- **Lazy consensus** governs routine work. A proposal — a pull request,
  or an issue proposing a change — proceeds when no maintainer objects
  within a reasonable review window; silence is consent. Routine changes
  do not require an explicit vote.
- **Objections** pause the change. The people involved discuss, and a
  maintainer decides once the discussion has run its course. If the
  maintainers disagree among themselves, the organization owner decides.
- **Owner approval is always required** for changes to: `LICENSE` or
  `NOTICE`; this document, `SECURITY.md`, or the Code of Conduct; the
  tool-profile contract under `contracts/`; the no-engine-dependency
  gate (`scripts/check-no-engine-dependency.py`); anything under
  `.github/` (CI, release automation, templates); and repository
  visibility or publishing configuration. `CODEOWNERS` routes these to
  deliberate review automatically.
- **Releases** are automated from Conventional Commits (see
  [`CONTRIBUTING.md`](./CONTRIBUTING.md#commit-messages-conventional-commits-required));
  no individual cuts ad-hoc releases outside that machinery. Publishing
  to PyPI and npm is gated on owner-controlled credentials.

## Becoming a maintainer

External contributors can become maintainers. The path:

1. **Contribute consistently** — merged pull requests, thoughtful
   reviews, and issue triage over a sustained period. Quality and
   judgment count more than volume; upholding the no-engine-dependency
   rule and the contract's safety invariants is non-negotiable.
2. **Nomination** — by an existing maintainer, or by self-nomination
   through an issue. The current maintainers discuss (privately where
   appropriate) and reach lazy consensus among themselves.
3. **Appointment** — the organization owner grants write access and
   adds the new maintainer to `CODEOWNERS`.

Maintainers may step down at any time. A maintainer inactive for an
extended period (roughly six months) may be moved to emeritus status —
with thanks, and with an open door to return. The organization owner may
remove a maintainer for cause, including Code of Conduct violations.

## Code of Conduct

All participation is governed by the [Code of
Conduct](./CODE_OF_CONDUCT.md) (Contributor Covenant 2.1). Report
conduct concerns to conduct@algenta.ai.

## Licensing

The project is Apache-2.0, inbound = outbound (GitHub Terms of Service
§D.6); there is no Contributor License Agreement. See
[`CONTRIBUTING.md`](./CONTRIBUTING.md#licensing).

## Changes to this document

Amendments require a pull request approved by the organization owner.
Substantive changes are announced in the pull request and left open for
comment for at least one week before merging.
