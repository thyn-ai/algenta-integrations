# Haystack x Algenta recipes

Runnable, test-covered Haystack `Pipeline` recipes that put [Algenta](https://algenta.ai) at the
center of the patterns Haystack developers actually build: component graphs that combine
read-only observation tools with governed execution and typed denial handling.

Every recipe runs against the package's own stub Algenta MCP server
(`tests/stub_server.py`, served over a real local HTTP socket) -- **no live engine, no LLM API
key, no network access beyond 127.0.0.1**.

## Setup

```bash
git clone https://github.com/thyn-ai/algenta-integrations
cd algenta-integrations/python
uv sync --all-packages --all-extras
cd haystack-algenta
```

## The recipes

| Recipe | What it shows | Run |
|---|---|---|
| `governed_pipeline` | A Haystack `Pipeline` that uses the `observe` profile (`recommend`) alongside `execute_decision`, with the typed denial hook wired as a custom component | `uv run python -m recipes.governed_pipeline` |

## Running the tests

Every recipe has its own test file (`recipes/test_<name>.py`), running the same real
stub-server round trips the scripts do:

```bash
# from algenta-integrations/python/haystack-algenta
uv run pytest recipes -v

# or the whole package suite (existing tests + recipes), as CI runs it:
cd .. && uv run pytest haystack-algenta -v
```

## Why a custom component instead of `build_algenta_governance_hooks()`?

`build_algenta_governance_hooks()` registers `GovernedReceiptHook` as an `Agent` `after_tool`
hook. A Haystack `Pipeline` has no hook seam, so `governed_pipeline.py` wraps `execute_decision`
in a custom `@component` that applies the same typed outcome parsing
(`extract_execution_outcome_from_tool_result`) and raises the same `AlgentaToolDenied` the Agent
hook does. The denial still propagates out of `pipeline.run()` unmodified.
