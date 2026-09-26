# LangChain x Algenta recipes

Ten runnable, test-covered recipes that put [Algenta](https://algenta.ai) at the center of
the patterns LangChain developers actually build: RAG, tool-calling agents, LangGraph
graphs, LCEL chains, retrieval tools, and eval gates. Every recipe:

- runs against the package's own stub Algenta MCP server (`tests/stub_server.py`, served
  over a real local HTTP socket by `recipes/_stub.py`) -- **no live engine, no LLM API
  key, no network access beyond 127.0.0.1**;
- drives a real LangChain loop (real `create_agent`, real `StateGraph`, real LCEL, real
  MCP wire) with a deterministic scripted chat model
  (`recipes/_support.ScriptedChatModel`) in place of the weights;
- is both a script (`uv run python -m recipes.<name>`) and an importable `run_*` function
  you can point at your own self-hosted engine's MCP base URL and a real chat model.

## Setup

```bash
git clone https://github.com/thyn-ai/algenta-integrations
cd algenta-integrations/python
uv sync --all-packages --all-extras   # the quickstart extra provides langchain + langgraph
cd langchain-algenta
```

## The recipes

| Recipe | What it shows | Run |
|---|---|---|
| `governed_rag` | RAG with a governed data path (`query_data`) and a decision-memory audit receipt per answer | `uv run python -m recipes.governed_rag` |
| `approval_gated_agent` | The agent proposes (`plan`/`log`); a human gate in application code approves; only then does app-held `execute` tooling run -- the model never sees `execute_decision` | `uv run python -m recipes.approval_gated_agent` |
| `simulation_agent_loop` | A `create_agent` tool loop that must call `simulate` + `recommend` before answering, on the read-only `observe` profile | `uv run python -m recipes.simulation_agent_loop` |
| `bm25_retrieval_tool` | A dependency-free BM25 search tool side by side with governed Algenta tools in one agent toolset, with the grounded answer logged for audit | `uv run python -m recipes.bm25_retrieval_tool` |
| `structured_denial_handling` | Mapping `execute_decision`'s real contract onto typed outcomes (`Delivered` / `DeliveryFailed` / `PolicyDenied`) and branching per named gate | `uv run python -m recipes.structured_denial_handling` |
| `decision_memory_agent` | Closing the feedback loop: agent logs decisions; app code records outcomes and reads the accuracy summary via the real registry's Decision Memory tools | `uv run python -m recipes.decision_memory_agent` |
| `policy_gated_langgraph_node` | A LangGraph `StateGraph` node that routes on the engine's synchronous answer -- delivered / failed delivery / blocked (per gate) -- as explicit graph edges | `uv run python -m recipes.policy_gated_langgraph_node` |
| `idempotent_tool_retry` | At-most-once execution from the caller's side: retry only transport errors; treat the `idempotency` gate as dedup, never retry policy denials | `uv run python -m recipes.idempotent_tool_retry` |
| `audit_trailed_chain` | An LCEL chain (`prompt \| model \| parser`) wrapped so every run is simulated, fingerprinted (SHA-256), and logged to decision memory | `uv run python -m recipes.audit_trailed_chain` |
| `deterministic_scoring` | An eval gate that ranks candidate outputs through the engine's deterministic composite `score`, gates on a threshold, and logs the selection | `uv run python -m recipes.deterministic_scoring` |

## Running the tests

Every recipe has its own test file (`recipes/test_<name>.py`), running the same real
stub-server round trips the scripts do:

```bash
# from algenta-integrations/python/langchain-algenta
uv run pytest recipes -v

# or the whole package suite (existing tests + recipes), as CI runs it:
cd .. && uv run pytest langchain-algenta -v
```

## Notes on profiles and the `full`-profile tools

The four contract tool profiles (`observe` / `govern` / `execute` / `full`) are described
in the [package README](../README.md#tool-profiles). Two recipes
(`decision_memory_agent`, `deterministic_scoring`) exercise real engine registry tools
that sit outside the contract profiles' named sets -- the Decision Memory reads
(`list_decisions`, `get_decision`, `record_outcome`) and the composite `score` tool. On a
real engine, exactly like on the recipe stub, those are reachable only through the opt-in
`full` profile; both recipes use them from application code (memory bookkeeping, an eval
harness), never as a model-facing default, and their tests assert the model-facing tool
list stays clean.

Algenta's accelerated `bm25_mojo` / `sacrebleu_mojo` kernels are not published to PyPI, so
the retrieval and scoring recipes ship dependency-free, deterministic stand-ins at the
documented seams -- swap in the kernels (or your own store/scorer) inside your deployment.
