"""Runnable LangChain x Algenta recipes -- see `recipes/README.md` for the full index.

Every module here is both a runnable script and an importable, test-covered function:

- Run one from this package's directory (`algenta-integrations/python/langchain-algenta`,
  after `uv sync --all-packages --all-extras` in `algenta-integrations/python`):

      uv run python -m recipes.governed_rag

- Or import its `run_*` function with your own live self-hosted engine's MCP base URL.

Every recipe drives the package's own stub Algenta MCP server (`tests/stub_server.py`,
served over a real local HTTP socket by `recipes._stub`) and a deterministic scripted chat
model (`recipes._support.ScriptedChatModel`), so all of them run with zero credentials, no
live engine, and no network access beyond 127.0.0.1. Swap the stub base URL for your own
engine's and the scripted model for a real chat model to take any of them to production.
"""
