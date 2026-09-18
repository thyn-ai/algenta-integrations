# Support

## Where to go with what

| I need to… | Go here |
|---|---|
| Report a bug in an integration package | [GitHub Issues](https://github.com/thyn-ai/algenta-integrations/issues) — *Bug report* template |
| Request a new capability or framework integration | [GitHub Issues](https://github.com/thyn-ai/algenta-integrations/issues) — *Integration capability request* template |
| Ask a usage question | [GitHub Discussions](https://github.com/thyn-ai/algenta-integrations/discussions) — keeps the answer searchable for the next person |
| Chat with the community | [Discord](https://algenta.ai/discord) |
| Get help as a paying customer | <https://algenta.ai/support> — your existing support channel: faster, covered by your plan's SLA, and keeps account-specific details private |
| Report a security vulnerability | **Never in public.** See [`SECURITY.md`](./SECURITY.md) — a private GitHub Security Advisory, or security@algenta.ai |

Before filing, please check the root [`README`](./README.md) (per-package
status and known limits) and search existing issues and discussions.

## What belongs in this repository — and what doesn't

This repository covers the framework-integration packages only. Routing
adjacent topics to the right place gets you a faster, better answer:

- **The `algenta-sdk` client libraries** — connection handling,
  authentication, the SDK's own API — belong in
  [`thyn-ai/algenta-sdk`](https://github.com/thyn-ai/algenta-sdk), not
  here.
- **The Algenta engine itself** (closed source) belongs with your
  enterprise support channel; for engine security concerns,
  security@algenta.ai.

## Response expectations

Community support is best-effort: there is no guaranteed response time
or SLA on Issues, Discussions, or Discord. The maintainers are a small
team, and a well-prepared report — package and version, a minimal
reproduction, expected versus actual behavior — is the single biggest
factor in getting a fast answer. Paying customers receive support under
their plan's own SLA via <https://algenta.ai/support>.

Security reports follow the response commitments in
[`SECURITY.md`](./SECURITY.md#what-to-expect) — those are the only
timelines this project commits to publicly.
