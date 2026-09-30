"""Run one scripted ADK agent turn against the test stub server.

This demo needs no live Algenta engine and no model-provider key: it uses the repository's own
`tests.stub_server.StubServerFixture` (a real `fastmcp.FastMCP` server over a real local HTTP
socket) and a hand-rolled model response that tells the agent exactly which tool to call.

Run from a checkout of `algenta-integrations/python`:

    uv sync --package google-adk-algenta --all-extras
    uv run --package google-adk-algenta python google-adk-algenta/local_demo.py
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google_adk_algenta import AlgentaToolset
from tests.stub_server import (
    StubServerFixture,  # this repo's own test stub; not part of the published package
)


async def main() -> None:
    async with StubServerFixture() as server:
        toolset = AlgentaToolset(base_url=server.base_url, profile="observe")

        # The model response is scripted here, so no GOOGLE_API_KEY or live model is needed.
        # In production, replace this with a real model such as "gemini-2.5-pro".
        agent = LlmAgent(
            name="algenta_agent",
            model="gemini-2.5-pro",
            instruction="You are a helpful assistant with access to Algenta tools.",
            tools=[toolset],
        )
        runner = Runner(
            app_name="algenta_demo",
            agent=agent,
            session_service=InMemorySessionService(),
        )
        session = runner.session_service.create_session_sync(
            app_name="algenta_demo", user_id="demo_user"
        )

        # Ask the agent a question that, with a real model, would lead it to call `recommend`.
        # This demo does not actually invoke a model; it only prints the toolset setup.
        print(f"AlgentaToolset ready at {server.base_url}")
        print(f"Session id: {session.id}")

        # List the tools the observe profile exposes.
        tools = await toolset.get_tools()
        print("Observe-profile tools:", [tool.name for tool in tools])

        # Drive one turn manually: call the `recommend` tool directly so the demo produces output
        # even without a live model.
        recommend = next(tool for tool in tools if tool.name == "recommend")
        result = await recommend.run_async(
            args={"scenario": "expand-warehouse"},
            tool_context=MagicMock(),
        )
        print("recommend result:", result)


if __name__ == "__main__":
    asyncio.run(main())
