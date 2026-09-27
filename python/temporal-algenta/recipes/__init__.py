"""Recipe index for `temporal-algenta`.

Each module is one self-contained, runnable recipe: a real Temporal workflow (plus the
worker/client wiring to run it) putting Algenta's governed decision execution at the center of
a popular Temporal pattern. Every recipe runs end-to-end with zero credentials:

    cd python/temporal-algenta
    uv run python -m recipes.durable_decision_workflow

Without `ALGENTA_BASE_URL` set, each recipe starts its own deterministic demo engine
(`recipes/demo_engine.py`) and a local time-skipping Temporal test server -- no engine, no
Temporal install, no API keys. With `ALGENTA_BASE_URL` (and optionally `TEMPORAL_ADDRESS`)
set, the same recipe runs against your self-hosted Algenta Engine and your own Temporal
server instead. See the package README for the full index with one-line value props.

The modules are imported by Temporal's workflow sandbox when a worker validates the workflow
definitions, so their module level imports only sandbox-safe modules plus an explicit
`workflow.unsafe.imports_passed_through()` block -- the pattern Temporal documents for
workflow modules that need activity-side imports. Heavy machinery (the demo engine, the
recipe runner) is imported inside `main()` function bodies, never at module level.
"""
