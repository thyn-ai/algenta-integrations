# Governed support-triage agent (LangGraph)

A minimal, runnable example of the observe → govern → approve → execute → persist
arc for a support-ticket queue, built on [`langchain-algenta`](../../langchain-algenta/)
and LangGraph.

- **observe**: read-only Algenta tools (`recommend`) look at the ticket.
- **govern**: planning/recording tools (`plan_decision`, `log_decision`) produce a
  logged decision; `execute_decision` is deliberately absent from both tool lists.
- **approve**: a caller-supplied gate decides whether to proceed.
- **execute**: application code, not the model, holds the `execute`-profile tool.
- **persist**: the resulting `ExecutionReceipt` is written to a local directory.

No proprietary service or LLM API key is required to run the example locally.

## Run locally (no self-hosted engine, no API key)

From the workspace root:

```bash
cd algenta-integrations/python
uv sync --all-packages --all-extras
uv run --package support-triage python -m support_triage
```

The command starts a deterministic stub Algenta MCP server on `127.0.0.1`, runs
two demo tickets through the graph, and writes any receipts to `./receipts/`.

## Run against your own self-hosted engine

Set `ALGENTA_BASE_URL` to your engine's MCP endpoint. The stub server is skipped
and the example prompts for per-decision approval:

```bash
export ALGENTA_BASE_URL=https://your-engine.example.com/mcp
uv run --package support-triage python -m support_triage
```

## Run the tests

```bash
cd algenta-integrations/python
uv run --package support-triage pytest examples/support-triage -v
```

The test suite uses the same real MCP stub server as the local demo, with
injected approval gates, so every run is deterministic and requires no network.
