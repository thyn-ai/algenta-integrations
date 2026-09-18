"""A real, runnable demo of `AlgentaToolset` that needs neither a live self-hosted Algenta
Engine nor a model provider API key.

See the "Try it locally" section of README.md for context. This script:

1. Starts `tests/stub_server.py`'s stub Algenta MCP server -- a real `fastmcp.FastMCP` server
   over a real local HTTP socket, standing in for a self-hosted Algenta Engine.
2. Points a real `AlgentaToolset` at it.
3. Drives a real `pydantic_ai.Agent` turn against it using
   `pydantic_ai.models.test.TestModel`, which scripts tool-calling deterministically without
   calling any LLM provider.

Not part of the published `pydantic-ai-algenta` PyPI package -- it imports `tests.stub_server`,
which only exists in a checkout of this repository. Run it from this directory:

    uv run --package pydantic-ai-algenta python local_demo.py

(from the `python/` workspace directory), after `uv sync --package pydantic-ai-algenta --all-extras`.
"""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai_algenta import AlgentaToolset
from tests.stub_server import StubServerFixture


async def main() -> None:
    async with StubServerFixture() as server:
        # profile="observe" -- the default -- exposes only get_contract/query_data/simulate/recommend.
        toolset = AlgentaToolset(base_url=server.base_url, profile="observe")

        # TestModel scripts which tool the agent calls, deterministically, with no network call
        # to any LLM provider. Swap in a real model (e.g. Agent("openai:gpt-5", ...)) once you
        # have both a live self-hosted engine and a model API key -- see README.md's Quick start.
        agent = Agent(TestModel(call_tools=["recommend"]), toolsets=[toolset])

        result = await agent.run("What should we do about scenario X?")
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
