# Conformance demo fixtures (planned — D9)

This directory is a placeholder. It does not contain any fixtures yet.

## Intended shape

Per the approved integrations plan (§9), this directory will eventually hold
a set of **12 conformance scenarios** — runnable, framework-agnostic fixtures
that exercise every tool profile (`observe`, `govern`, `execute`, `full`)
defined in
[`../contracts/integration-tool-contract.json`](../contracts/integration-tool-contract.json)
against a real (test-mode) self-hosted Algenta Engine. Each scenario is
expected to:

- Declare which profile it exercises and which tool(s) from that profile it
  calls.
- Provide the exact model-facing tool-call payload(s) a conforming
  integration package should produce for a given natural-language prompt.
- Provide the expected engine response shape (or a recorded fixture
  response, for offline/CI runs that don't hit a live engine).
- For `execute`-tier scenarios specifically: assert that the call cannot
  succeed without every gate in the contract's
  `profiles.execute.requires_all_of` being satisfied (enablement, role
  check, policy approval, idempotency key, `plan_hash`, nonce), and assert
  that `force` / `override_safety` never appear in the model-facing payload.

Each of `python/*/tests/` and `typescript/algenta-tools/**/*.test.ts` is
expected to run the same 12 scenarios against its own package once that
package exists, via `scripts/check-parity.py` / `scripts/check-parity.ts`
(see those scripts' TODOs), so that "conforms to the contract" means the
same thing in every language.

## What's NOT here yet

- The 12 scenario fixtures themselves (data, prompts, expected payloads).
- A scenario runner/harness.
- Any wiring from `scripts/check-parity.{py,ts}` into this directory (those
  scripts are stubs today — see their own docstrings/headers).

Building the fixture set is explicitly out of scope for the D0 bootstrap
that created this directory. It is tracked as **D9** in the roadmap (see the
root [README.md](../README.md#status--roadmap)).
