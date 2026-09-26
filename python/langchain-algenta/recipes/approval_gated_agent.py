"""Approval-gated agent execution: the agent proposes, the application approves, then executes.

Agents that can take real-world actions are LangChain's hottest pattern -- and the one
security teams push back on hardest. This recipe shows the honest, enforceable shape of
human approval around such an agent:

1. The agent runs with the `govern` tool profile only -- it can `plan_decision` and
   `log_decision`, but `execute_decision` is not in its tool list at all. There is nothing
   for a prompt-injection to talk into executing; the capability simply isn't there.
2. The application -- not the model -- owns a second, `execute`-profile tool handle.
3. A human (or an automated policy function) approves the *logged* decision. Only on
   approval does application code call `execute_decision`, and it gets back a real
   `ExecutionReceipt`.

Why the approval lives in application code: the engine's `execute_decision` commits
synchronously (a receipt, or a denial on one of three named gates, in the same call), and
the engine's separate plan_hash+nonce human-approval system is intentionally not exposed
as an MCP/LLM tool -- no MCP integration can observe it. So the correct place to gate
execution is around the agent, exactly as shown here. See the package README's "The real
`execute_decision` denial model" for the full account.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.approval_gated_agent
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_algenta import ExecutionReceipt, create_algenta_tools, parse_receipt
from langchain_core.messages import AIMessage

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, first_text_json, tool_by_name, tool_message_payloads

#: An approval gate receives the logged decision record and returns whether execution may
#: proceed. Tests inject a scripted gate; the CLI main uses an interactive prompt.
ApprovalGate = Callable[[dict[str, Any]], Awaitable[bool]]


def _proposal_script(scenario: str, chosen_action: str) -> list[AIMessage]:
    return [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "plan_decision", "args": {"scenario": scenario}, "id": "call_plan", "type": "tool_call"},
                {"name": "log_decision", "args": {"chosen_action": chosen_action}, "id": "call_log", "type": "tool_call"},
            ],
        ),
        AIMessage(
            content=f"I planned and logged the {chosen_action!r} decision. Execution awaits human approval."
        ),
    ]


async def run_approval_gated_agent(
    base_url: str,
    *,
    scenario: str,
    chosen_action: str,
    webhook_url: str,
    approval_gate: ApprovalGate,
) -> dict[str, Any]:
    """Run the propose-approve-execute flow; return the proposal, verdict, and receipt (if any)."""
    from langchain.agents import create_agent

    # The model-facing handle: govern profile. `execute_decision` is genuinely absent.
    agent_tools = await create_algenta_tools(base_url=base_url, profile="govern")
    agent = create_agent(ScriptedChatModel(script=_proposal_script(scenario, chosen_action)), tools=agent_tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": f"Prepare the decision for {scenario}."}]})

    logged_records = tool_message_payloads(result["messages"], name="log_decision")
    if not logged_records:
        raise RuntimeError("The agent never logged a decision; there is nothing to approve.")
    decision_record = logged_records[-1]

    approved = await approval_gate(decision_record)
    receipt: ExecutionReceipt | None = None
    if approved:
        # The application-only handle: execute profile. The model never sees this list.
        execute_tools = await create_algenta_tools(base_url=base_url, profile="execute")
        execute_decision = tool_by_name(execute_tools, "execute_decision")
        raw = await execute_decision.ainvoke({"decision_id": decision_record["decision_id"], "webhook_url": webhook_url})
        receipt = parse_receipt(first_text_json(raw))
        if receipt is None:
            raise RuntimeError(f"execute_decision returned an unrecognized payload: {raw!r}")

    return {
        "decision_id": decision_record["decision_id"],
        "approved": approved,
        "receipt": receipt,
        "agent_final_message": result["messages"][-1].content,
    }


async def _interactive_approval(decision_record: dict[str, Any]) -> bool:
    answer = input(f"Approve execution of {decision_record['decision_id']!r}? [y/N] ")
    return answer.strip().lower() == "y"


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_approval_gated_agent(
            base_url,
            scenario="expand-eu",
            chosen_action="expand-eu",
            webhook_url="https://example.com/hook",
            approval_gate=_interactive_approval,
        )
        print("Agent:", result["agent_final_message"])
        if result["approved"] and result["receipt"] is not None:
            print(f"Executed: {result['receipt'].decision_id} -> {result['receipt'].execution_status}")
        else:
            print(f"Execution withheld for {result['decision_id']} (not approved).")


if __name__ == "__main__":
    asyncio.run(main())
