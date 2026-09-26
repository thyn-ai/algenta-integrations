"""Decision memory for agents: log the decision, record the outcome, measure accuracy.

Agent memory is a top LangChain theme -- but "what did the agent *decide*, and did it work
out?" is the memory enterprises actually audit. This recipe closes that loop with the
engine's real Decision Memory surface:

1. The agent (model-facing, `govern` profile) logs its decision via `log_decision` and gets
   back a `decision_id`.
2. Application code -- never the model -- uses the real registry's Decision Memory reads
   (`record_outcome`, `list_decisions`, `get_decision`) to close the feedback loop: record
   what actually happened, then read the accuracy summary (`outcome_delta =
   actual_outcome - expected_value`) to see whether decisions are beating their forecasts.

Those read tools sit outside the four contract profiles' named sets, so -- exactly like on
a real engine -- they're reachable only via the opt-in `full` profile. This recipe keeps
the boundary explicit: two handles, `govern` for the model, `full` for application
bookkeeping, and the test asserts the memory reads are *not* in the model-facing list.
That curation is the pattern: `full` is for admin/ops code, never a model-facing default.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.decision_memory_agent
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_algenta import create_algenta_tools
from langchain_core.messages import AIMessage

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, first_text_json, tool_by_name, tool_message_payloads

#: The Decision Memory reads the application uses. Kept as a module constant so the test
#: can assert none of them ever appears in the model-facing (govern) tool list.
MEMORY_READ_TOOLS = ("list_decisions", "get_decision", "record_outcome")


def _script(chosen_action: str) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "log_decision", "args": {"chosen_action": chosen_action}, "id": "call_log", "type": "tool_call"}
            ],
        ),
        AIMessage(content=f"Logged the {chosen_action!r} decision to decision memory."),
    ]


async def run_decision_memory_agent(base_url: str, *, chosen_action: str, actual_outcome: float) -> dict[str, Any]:
    """Run the log -> record -> measure loop; return the decision id and accuracy summary."""
    from langchain.agents import create_agent

    # Model-facing handle: the agent can log decisions, but cannot read memory or execute.
    agent_tools = await create_algenta_tools(base_url=base_url, profile="govern")
    agent = create_agent(ScriptedChatModel(script=_script(chosen_action)), tools=agent_tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "Decide and log it."}]})

    logged_records = tool_message_payloads(result["messages"], name="log_decision")
    if not logged_records:
        raise RuntimeError("The agent never logged a decision.")
    decision_id = logged_records[-1]["decision_id"]

    # Application-only handle: the full registry, for bookkeeping code -- never handed to
    # the model. `full` exposes everything the stub engine advertises; we take only the
    # three Decision Memory reads this recipe uses.
    ops_tools = await create_algenta_tools(base_url=base_url, profile="full")
    record_outcome = tool_by_name(ops_tools, "record_outcome")
    list_decisions = tool_by_name(ops_tools, "list_decisions")
    get_decision = tool_by_name(ops_tools, "get_decision")

    outcome = first_text_json(
        await record_outcome.ainvoke({"decision_id": decision_id, "actual_outcome": actual_outcome})
    )
    memory = first_text_json(await list_decisions.ainvoke({"with_outcome_only": True}))
    record = first_text_json(await get_decision.ainvoke({"decision_id": decision_id}))

    return {
        "decision_id": decision_id,
        "outcome_delta": outcome["outcome_delta"],
        "memory_record": record,
        "accuracy_summary": memory["accuracy_summary"],
        "with_outcome_total": memory["total"],
    }


async def main() -> None:
    async with serve_recipe_stub(decision_memory=True) as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_decision_memory_agent(base_url, chosen_action="expand-eu", actual_outcome=47.5)
        print(f"Logged decision: {result['decision_id']}")
        print(f"Outcome delta: {result['outcome_delta']} (actual minus expected)")
        print(f"Accuracy summary: {result['accuracy_summary']}")
        print(f"Decisions with recorded outcomes: {result['with_outcome_total']}")


if __name__ == "__main__":
    asyncio.run(main())
