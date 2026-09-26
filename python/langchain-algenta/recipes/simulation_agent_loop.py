"""Simulation-first agent loop: a tool-calling agent that must simulate before it answers.

The most reliable agent pattern on LangChain is "look before you leap": instead of letting
the model free-associate a recommendation, require the loop to ground it in a simulation
and a governed recommendation first. This recipe builds exactly that agent with
`langchain.agents.create_agent`:

1. the agent calls `simulate` for the scenario's decision envelope (expected value,
   confidence),
2. then `recommend` for the governed recommended action,
3. then answers -- citing numbers that came from tool results, not from the model.

The whole loop runs on the read-only `observe` profile: four tools, nothing that writes,
plans, or executes. That is the right default for any agent whose job is advisory.

The scripted model makes the loop deterministic and credential-free; the agent runtime,
tool calling, and MCP round trips are entirely real. Swap in any chat model -- the tool
contract the agent sees is unchanged.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.simulation_agent_loop
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_algenta import create_algenta_tools
from langchain_core.messages import AIMessage

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, tool_message_payloads


def _script(scenario: str) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "simulate", "args": {"scenario": scenario}, "id": "call_sim", "type": "tool_call"},
                {"name": "recommend", "args": {"scenario": scenario}, "id": "call_rec", "type": "tool_call"},
            ],
        ),
        AIMessage(
            content=(
                f"For {scenario!r}, the simulation's expected value is 42.0 and the governed "
                "recommendation is 'hold' at 0.87 confidence. I recommend holding."
            )
        ),
    ]


async def run_simulation_agent_loop(base_url: str, scenario: str) -> dict[str, Any]:
    """Run the simulation-first agent loop; return the final answer and both tool payloads."""
    from langchain.agents import create_agent

    tools = await create_algenta_tools(base_url=base_url, profile="observe")
    agent = create_agent(ScriptedChatModel(script=_script(scenario)), tools=tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": f"Should we proceed with {scenario}?"}]})

    simulations = tool_message_payloads(result["messages"], name="simulate")
    recommendations = tool_message_payloads(result["messages"], name="recommend")
    if not simulations or not recommendations:
        raise RuntimeError("The agent answered without simulating first -- the loop contract was broken.")

    return {
        "final_message": result["messages"][-1].content,
        "simulation": simulations[-1],
        "recommendation": recommendations[-1],
        "tool_names": sorted(tool.name for tool in tools),
    }


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_simulation_agent_loop(base_url, "expand-eu")
        print("Observe-profile tools:", result["tool_names"])
        print("Simulation:", result["simulation"])
        print("Recommendation:", result["recommendation"])
        print("Agent:", result["final_message"])


if __name__ == "__main__":
    asyncio.run(main())
