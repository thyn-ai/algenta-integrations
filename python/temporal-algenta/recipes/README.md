# temporal-algenta recipes

Ten runnable recipes putting Algenta's governed decision execution at the center of
Temporal's most popular patterns. Each recipe is a real Temporal workflow module with its own
`main()`; each runs end-to-end with **zero credentials** (it starts a deterministic demo
engine — `recipes/demo_engine.py` — and a local time-skipping Temporal test server for you),
and each has a test in `tests/`.

With `ALGENTA_BASE_URL` (and optionally `TEMPORAL_ADDRESS` / `TEMPORAL_NAMESPACE`) set, the
same recipe runs against your self-hosted Algenta Engine and your own Temporal server
instead.

Run any of them from a checkout:

```bash
cd python
uv sync --all-packages --all-extras
cd temporal-algenta && uv run python -m recipes.durable_decision_workflow
```

| Recipe | One line | Run |
|---|---|---|
| [`durable_decision_workflow.py`](./durable_decision_workflow.py) | The canonical durable workflow: `log_decision` → `execute_decision`, each step a retried activity; the workflow result *is* the typed execution receipt. | `uv run python -m recipes.durable_decision_workflow` |
| [`human_approval_workflow.py`](./human_approval_workflow.py) | Policy denial parks the workflow on a durable timer; an operator signal (Temporal's #1 human-in-the-loop primitive) approves with an explicit break-glass override; the receipt shows `safety_overridden=True`. | `uv run python -m recipes.human_approval_workflow` |
| [`idempotent_activity_receipt_dedup.py`](./idempotent_activity_receipt_dedup.py) | "Safe to retry", answered: the engine's idempotency gate turns a re-delivered attempt into a deduplicated success — never a double delivery, never a spurious failure. | `uv run python -m recipes.idempotent_activity_receipt_dedup` |
| [`scheduled_governed_simulation.py`](./scheduled_governed_simulation.py) | A real Temporal `Schedule` (cron) firing a nightly governed simulation into decision memory. | `uv run python -m recipes.scheduled_governed_simulation` |
| [`saga_with_policy_gates.py`](./saga_with_policy_gates.py) | Temporal's flagship saga pattern with a policy denial as the trigger: reverse-order compensations recorded in decision memory, returned as a deterministic `SagaReport`. | `uv run python -m recipes.saga_with_policy_gates` |
| [`retry_policy_structured_denials.py`](./retry_policy_structured_denials.py) | `RetryPolicy` meets governance: transient engine errors ride out the backoff; policy denials fail on attempt one, the structured denial intact all the way to the client. | `uv run python -m recipes.retry_policy_structured_denials` |
| [`durable_decision_memory.py`](./durable_decision_memory.py) | The long-running entity workflow: decisions journaled via signals, read via queries, persisted to Algenta decision memory. | `uv run python -m recipes.durable_decision_memory` |
| [`batch_simulation_pipeline.py`](./batch_simulation_pipeline.py) | Parallel fan-out (`asyncio.gather` over activities) with deterministic aggregation and a BM25 retrieval-ranking stage — with **live-measured speedups** (fan-out vs sequential loop; BM25 vs naive recount). | `uv run python -m recipes.batch_simulation_pipeline` |
| [`agent_workflow_governed_tools.py`](./agent_workflow_governed_tools.py) | The durable AI agent loop with a profile-filtered tool surface: under `govern` the agent may plan but `execute_decision` isn't advertised — refused before any call. | `uv run python -m recipes.agent_workflow_governed_tools` |
| [`audit_trail_query_workflow.py`](./audit_trail_query_workflow.py) | A live-queryable, sequence-numbered audit trail reconciled with the engine's own audit dataset via `query_data`. | `uv run python -m recipes.audit_trail_query_workflow` |

## Speed notes

- **`batch_simulation_pipeline`** prints live-measured numbers on every run: the parallel
  fan-out vs a naive sequential activity loop (same worker, same engine, same run), and the
  BM25 retrieval-ranking step vs a naive per-document token recount.
- The BM25 step uses Algenta's `bm25_mojo` kernel transparently when it is importable
  (`pip install bm25-mojo`), and otherwise the deterministic pure-Python stand-in shipped in
  `recipes/_kernels.py` — identical Okapi BM25 scoring either way. The kernel is an *optional,
  undeclared* runtime extra on purpose (this repository's dependency gate keeps engine-adjacent
  packages out of manifests), and as of this writing it is not yet on PyPI, so the stand-in is
  what runs. The recipe prints which implementation produced its numbers (`kernel in use: ...`).
