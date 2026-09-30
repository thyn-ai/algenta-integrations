# Cross-framework conformance suite

One fixture set, one runner, every integration in this repo held to the same evidence. This suite
**is** the "Validated integration" gate: no package should carry that label without a passing run.

## Why this exists, stated plainly

Every package here was already described as "conformance-verified" while this suite did not exist.
The only file carrying the name — `python/litellm-algenta/tests/test_gateway_conformance.py` — runs
against a `stub_server.py`, so it verified the stub. That is how a claim outruns its evidence: the
gate was planned, the label was applied, and nothing connected the two.

So this suite is deliberately built to be **falsifiable**, and its honesty is demonstrated rather
than asserted (see *Proof the gate works* below).

## Running it

```bash
ALGENTA_BASE_URL=http://localhost:8000 \
ALGENTA_API_KEY=your-key \
python -m demo.conformance.runner --json evidence.json
```

Run the same scenarios through the LangChain adapter:

```bash
ALGENTA_BASE_URL=http://localhost:8000 \
ALGENTA_API_KEY=your-key \
python -m demo.conformance.runner --adapter langchain --json evidence-langchain.json
```

There is **no default base URL** on purpose. A default pointing anywhere other than your own engine
is how "self-hosted" quietly stops being true. The runner exits `2` without one.

Exit codes: `0` all exercisable scenarios passed · `1` at least one failed · `2` misconfigured.

## The 12 scenarios, and which actually run

Measured against a current Algenta engine build over HTTP with a real Postgres — **9 exercisable, 3 not**. The LangChain adapter exercises the same HTTP surface through LangChain `StructuredTool` invocations and produces the same honest breakdown.

| # | Scenario | Direct HTTP | LangChain adapter |
|---|---|---|---|
| 1 | Successful read-only recommendation | pass | pass |
| 2 | Execution denied by policy (409 named gate) | pass | pass |
| 3 | Execution paused for approval | **blocked** | **blocked** |
| 4 | Approval granted, execution resumed | pass | pass |
| 5 | Approval rejected | pass | pass |
| 6 | Duplicate execution prevented | pass | pass |
| 7 | Expired / reused approval rejected | pass | pass |
| 8 | Modified `plan_hash` rejected | pass | pass |
| 9 | Upstream timeout as typed retryable error | **blocked** | **blocked** |
| 10 | Execution receipt (versioned envelope) | pass | pass |
| 11 | Audit export whose `X-Content-SHA256` verifies | pass | pass |
| 12 | Replay produces the same deterministic result | **blocked** | **blocked** |

### Why three do not run, and what would change that

**3 — Execution paused for approval.** The engine *refuses* rather than *pauses*: executing an
unapproved plan returns `409 plan_not_approved` and the plan stays `proposed`. There is no suspended
execution to resume and no continuation token, because approval is a separate prior call
(propose → approve → execute). That is a sound design, but it is not pause/resume, and asserting
pause/resume would be asserting a design intention. The scenario becomes exercisable once the
engine's Responses-protocol approval-required event ships.

**9 — Upstream timeout.** Needs a dependency that genuinely times out. This path talks only to
Postgres; provoking a real timeout needs a fault-injection proxy or the engine's compute path
under real load. Patching a client to raise would test the mock, not the engine.

**12 — Replay determinism.** Determinism is a property of the engine's compute kernels, and this
configuration runs with the accelerated compute runtime disabled (`ALGENTA_SKIP_MOJO_RUNTIME=1`).
Comparing two runs of the execute path in that configuration would compare an artifact list to
itself.

9 and 12 belong in a scheduled live tier against a fully booted engine stack.

**A blocked scenario is not a pass.** The runner reports `9 passed, 0 failed, 3 blocked` and prints
each reason. `pytest.skip` was avoided deliberately — "10 passed, 2 skipped" reads like success.

## Proof the gate works

A suite that cannot fail is decoration. The same runner was pointed at two engines:

| engine | result | exit |
|---|---|---|
| `main` (with the audit export, receipt envelope and named error codes) | **9 passed, 0 failed** | `0` |
| `main` before those three landed | **2 passed, 7 failed** | `1` |

The failures were diagnostic, not vague:

- scenarios 2, 5, 8 → `code=http_error`, the named gate flattened into prose
- scenario 10 → all seven receipt checks failed; there was no envelope
- scenario 11 → `404`, the export route did not exist

Both runs are committed under `demo/conformance/evidence/`. The passing run alone would prove
nothing without the run that fails.

## Fixtures

`demo/fixtures/expected.json` carries the semantic evidence every framework example must reproduce,
including the named error code each gate returns. It was **generated from a live run**, not authored
by hand, and records the engine build and configuration it was observed against. A hand-written
expectation is indistinguishable from a wish — regenerate it rather than editing it.

One value there is worth noticing: scenario 7 (reused approval challenge) returns
`plan_not_approvable`, not `invalid_nonce`. After a successful approval the plan leaves `proposed`,
so the lifecycle gate answers before the nonce is ever compared. The nonce *is* single-use; the code
simply names the earlier gate. Asserting `invalid_nonce` there would have been asserting a guess.

## What is still missing

- Per-framework adapters for the remaining integrations: Pydantic AI, LiteLLM, Haystack, LlamaIndex,
  MAF, AI SDK, Temporal. The LangChain adapter now proves the pattern; each sibling package needs an
  equivalent adapter that routes the same scenario set through its own client machinery.
- Cassette replay (`ALGENTA_DEMO_REPLAY=1`) for offline CI. Cassettes must be **recorded** from live
  runs — a hand-written cassette is the same fiction one layer down.
- The nightly live tier that unblocks 9 and 12.

Until all per-framework adapters exist and have live evidence, no package in this repo should be
labelled "Validated integration".
