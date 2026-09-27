# temporal-algenta

[![PyPI](https://img.shields.io/pypi/v/temporal-algenta.svg)](https://pypi.org/project/temporal-algenta/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../README.md)

[Temporal.io](https://temporal.io) integration for [Algenta](https://algenta.ai):
`AlgentaActivities`, your own self-hosted Algenta engine's MCP tool surface as durable
Temporal activities, plus the governance story durable execution actually needs:

- **Typed receipts across the wire** -- a successful `execute_decision` activity returns an
  `ExecutionReceiptData` (the engine's real receipt envelope as a sandbox-safe dataclass), so
  "did the governed action land?" is answered by the workflow history itself.
- **Structured denials as typed, non-retryable failures** -- a blocked `execute_decision`
  raises a `temporalio.exceptions.ApplicationError` whose `type` is the engine's own denial
  code (`execution_blocked_confidence`, `execution_blocked_risk_floor`,
  `execution_blocked_idempotency`), `non_retryable=True` (a policy denial is deterministic;
  retrying it can only deny again), and the full `{"code", "gate", "message", "override_hint"}`
  body in its details -- recoverable in workflow code via `denial_from_activity_error`.
- **Idempotency via receipts** -- Temporal retries activities; the engine's own idempotency
  gate dedups deliveries by `decision_id`. A retried delivery surfaces as a typed
  `execution_blocked_idempotency` failure, which is *positive proof* the side effect already
  happened -- exactly-once effect, auditable in history (see
  [`recipes/idempotent_activity_receipt_dedup.py`](./recipes/idempotent_activity_receipt_dedup.py)).
- **Human approval via signals** -- the engine intentionally exposes no approval round trip
  over MCP (its real plan_hash+nonce approval system is not an MCP tool), so this package
  builds approvals from Temporal's own primitives: a workflow parks on a typed denial, exposes
  it via `@workflow.query`, and only an operator's `@workflow.signal` can set
  `override_safety=True` for the retry -- the exact operator/break-glass path the shared
  contract reserves that field for (see
  [`recipes/human_approval_workflow.py`](./recipes/human_approval_workflow.py)).
- **Tool-profile enforcement** -- `observe` (read-only, the default), `govern`, `execute`, or
  the opt-in `full` registry, per
  [`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json),
  enforced at call time inside every activity.

## Install

```bash
pip install temporal-algenta
```

Real runtime dependencies: `temporalio` (the Temporal Python SDK), `mcp` (the base Model
Context Protocol SDK -- the same SDK the test suite's stub engine is built on), and `pydantic`
(the receipt/denial models at the activity edge). Everything is validated against
`temporalio` 1.33.x.

### Why no `algenta-sdk` dependency

This package never imports it: `AlgentaActivities` talks to your own self-hosted engine over
its MCP endpoint, like the `haystack-algenta` / `maf-algenta` / `llamaindex-algenta` packages
in this repository. The only Algenta-owned package any package here may depend on is
`algenta-sdk`, and a package that doesn't import it doesn't declare it.

## Self-hosted-first

`AlgentaMcpClient` talks to **your own self-hosted Algenta engine** over its MCP endpoint --
never a hosted-by-Algenta cloud service. The endpoint resolves, in order, from:

1. `AlgentaActivities(base_url=...)`
2. the `ALGENTA_BASE_URL` environment variable
3. `http://localhost:8000/mcp` (your engine's default self-hosted bind)

## Quick start

```python
from datetime import timedelta

from temporalio import workflow
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from temporal_algenta import (
        AlgentaActivities,
        ExecuteDecisionInput,
        GovernedDecisionInput,
        LogDecisionInput,
    )


@workflow.defn
class GovernedDecisionWorkflow:
    @workflow.run
    async def run(self, input: GovernedDecisionInput):
        logged = await workflow.execute_activity(
            AlgentaActivities.log_decision,
            LogDecisionInput(chosen_action=input.action, rationale=input.rationale),
            start_to_close_timeout=timedelta(seconds=30),
        )
        # Returns a typed ExecutionReceiptData -- or raises a non-retryable
        # ApplicationError(type="execution_blocked_<gate>") carrying the structured denial.
        return await workflow.execute_activity(
            AlgentaActivities.execute_decision,
            ExecuteDecisionInput(decision_id=logged["decision_id"], webhook_url=input.webhook_url),
            start_to_close_timeout=timedelta(seconds=60),
        )


# One AlgentaActivities instance per worker; the profile gates every call it makes.
algenta = AlgentaActivities(profile="execute")  # ALGENTA_BASE_URL selects your engine
worker = Worker(client, task_queue="governed-decisions", workflows=[GovernedDecisionWorkflow],
                activities=algenta.all_activities())
```

Two Temporal-specific rules this package is built around:

- **Workflow code never imports `pydantic`.** Temporal's workflow sandbox cannot reload
  pydantic's Rust core, so the pydantic receipt/denial models live at the activity edge only
  (`temporal_algenta.receipts`); everything that crosses the workflow/activity boundary is a
  stdlib dataclass from `temporal_algenta.types`. Import activity modules inside
  `workflow.unsafe.imports_passed_through()` (as above) -- the pattern Temporal documents for
  workflow modules referencing activity definitions.
- **Denials are failures, not results.** There is no "pending approval" receipt state (there
  isn't one on the real engine either -- see `temporal_algenta.receipts`). A workflow that
  wants to react to a policy gate catches `ActivityError` and reads the denial off it:

```python
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from temporal_algenta import denial_from_activity_error

try:
    receipt = await workflow.execute_activity(...)
except ActivityError as err:
    denial = denial_from_activity_error(err)
    if denial is None:
        raise  # not a policy denial -- a real failure; let the workflow fail
    # denial.gate / denial.code / denial.message / denial.override_hint, all typed
```

## Recipes

Ten runnable recipes putting Algenta at the center of Temporal's most popular patterns. Each
is a real workflow module under [`recipes/`](./recipes) with its own `main()`; each runs
end-to-end with **zero credentials** (it starts a deterministic demo engine and a local
time-skipping Temporal test server for you), and each has a test in `tests/`. From a checkout:

```bash
cd python
uv sync --all-packages --all-extras
uv run python -m recipes.durable_decision_workflow   # run from python/temporal-algenta
```

With `ALGENTA_BASE_URL` (and optionally `TEMPORAL_ADDRESS` / `TEMPORAL_NAMESPACE`) set, the
same recipe runs against your self-hosted engine and your own Temporal server instead.

| Recipe | What it shows | Run |
|---|---|---|
| [`durable_decision_workflow`](./recipes/durable_decision_workflow.py) | The canonical durable workflow: `log_decision` -> `execute_decision`, each step a retried activity; the workflow result *is* the typed receipt. | `uv run python -m recipes.durable_decision_workflow` |
| [`human_approval_workflow`](./recipes/human_approval_workflow.py) | Policy denial parks the workflow; an operator signal (Temporal's #1 human-in-the-loop primitive) approves with an explicit break-glass override; receipt shows `safety_overridden=True`. | `uv run python -m recipes.human_approval_workflow` |
| [`idempotent_activity_receipt_dedup`](./recipes/idempotent_activity_receipt_dedup.py) | The "safe to retry" question, answered: the engine's idempotency gate turns a re-delivered attempt into a deduplicated success, never a double delivery. | `uv run python -m recipes.idempotent_activity_receipt_dedup` |
| [`scheduled_governed_simulation`](./recipes/scheduled_governed_simulation.py) | A real Temporal `Schedule` (cron) firing a nightly governed simulation into decision memory. | `uv run python -m recipes.scheduled_governed_simulation` |
| [`saga_with_policy_gates`](./recipes/saga_with_policy_gates.py) | Temporal's flagship saga pattern with a policy denial as the trigger: reverse-order compensations recorded in decision memory. | `uv run python -m recipes.saga_with_policy_gates` |
| [`retry_policy_structured_denials`](./recipes/retry_policy_structured_denials.py) | `RetryPolicy` meets governance: transient engine errors ride out the backoff; policy denials fail on attempt one, denial intact to the client. | `uv run python -m recipes.retry_policy_structured_denials` |
| [`durable_decision_memory`](./recipes/durable_decision_memory.py) | The long-running entity workflow: decisions journaled via signals, read via queries, persisted to Algenta decision memory. | `uv run python -m recipes.durable_decision_memory` |
| [`batch_simulation_pipeline`](./recipes/batch_simulation_pipeline.py) | Parallel fan-out (`asyncio.gather` over activities) with a fully deterministic aggregation. | `uv run python -m recipes.batch_simulation_pipeline` |
| [`agent_workflow_governed_tools`](./recipes/agent_workflow_governed_tools.py) | The durable AI agent loop with a profile-filtered tool surface: under `govern` the agent may plan but `execute_decision` isn't advertised -- refused before any call. | `uv run python -m recipes.agent_workflow_governed_tools` |
| [`audit_trail_query_workflow`](./recipes/audit_trail_query_workflow.py) | A live-queryable, sequence-numbered audit trail reconciled with the engine's own audit dataset via `query_data`. | `uv run python -m recipes.audit_trail_query_workflow` |

## The denial taxonomy (what your workflow can catch)

Every governed call resolves to exactly one of these -- synchronously, in the same activity
attempt (there is no asynchronous "pending" outcome to wait on):

| Outcome | Temporal shape | Retryable? |
|---|---|---|
| Delivered | `ExecutionReceiptData` (`execution_status="delivered"`) | -- |
| Webhook target rejected delivery | `ExecutionReceiptData` (`execution_status="failed"`) -- the *call* succeeded | -- (a result, not a failure; react in workflow code) |
| Policy denial (`idempotency` / `confidence` / `risk_floor`) | `ApplicationError`, `type=execution_blocked_<gate>`, denial body in details | **No** (`non_retryable=True`) |
| Tool outside the configured profile | `ApplicationError`, `type="algenta_tool_denied_outside_profile"` | **No** (`non_retryable=True`) |
| Unclassified tool-execution error | `ApplicationError`, `type="algenta_tool_error"` | Yes -- bound it with your `RetryPolicy` |
| MCP session failure (wedged session, read timeout) | `ApplicationError`, `type="algenta_session_error"` | Yes -- a fresh session almost always clears it |
| Transport failure (engine unreachable, etc.) | the original exception, unwrapped | Yes (Temporal default) |

## Testing this package's own test suite

The suite runs a real [`mcp.server.fastmcp.FastMCP`](https://modelcontextprotocol.io/) server
(the *same* deterministic demo engine the recipes use -- `recipes/demo_engine.py`, one
implementation, no drift) over a real local HTTP socket, drives activities through Temporal's
real `ActivityEnvironment`, and runs every workflow inside the SDK's real time-skipping
`WorkflowEnvironment` (a local test server the SDK downloads and starts itself -- no Temporal
install, no credentials). Durable timers and schedules are fast-forwarded, so the suite tests
hour-long approval deadlines in milliseconds.

```bash
cd python
uv sync --all-packages --all-extras
uv run --package temporal-algenta pytest temporal-algenta/tests -v
```

## License

Apache-2.0. Copyright Algenta and contributors. See [LICENSE](./LICENSE).
