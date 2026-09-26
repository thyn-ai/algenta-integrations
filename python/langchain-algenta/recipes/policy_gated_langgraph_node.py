"""Policy-gated LangGraph node: route a graph on the engine's real execution gates.

LangGraph is LangChain's orchestration surface -- explicit nodes, explicit state, explicit
conditional edges. That explicitness is exactly what governed execution wants: a policy
denial is not an exception to log and swallow, it's a *route*. This recipe compiles a real
`StateGraph` whose execution node calls `execute_decision` and whose conditional edge
routes on the engine's synchronous answer:

- receipt with `execution_status="delivered"` -> `accept`
- receipt with `execution_status="failed"` (the webhook target said no -- a delivery
  problem, not a policy decision) -> `quarantine`
- `AlgentaExecutionBlocked` -> `review`, with the named gate (`idempotency`,
  `confidence`, `risk_floor`) carried in graph state for the reviewer

There is no "pending" route: the engine commits in the same call, so the graph never needs
a checkpointer-backed interrupt to learn the outcome. (Human approval, when you want it,
belongs in a separate approval step around the graph -- see `approval_gated_agent.py`.)

`langgraph` comes in via the package's `quickstart` extra (`pip install
"langchain-algenta[quickstart]"` pulls `langchain`, which pulls `langgraph`
transitively); `langchain-algenta` itself deliberately has no `langgraph` dependency.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.policy_gated_langgraph_node
"""

from __future__ import annotations

import asyncio
from typing import Any, TypedDict

from langchain_algenta import AlgentaExecutionBlocked, create_algenta_tools, parse_receipt

from ._stub import serve_recipe_stub
from ._support import first_text_json, tool_by_name


class ExecutionState(TypedDict, total=False):
    decision_id: str
    webhook_url: str
    outcome: str  # "delivered" | "failed" | "blocked"
    gate: str  # set when outcome == "blocked"
    verdict: str


def build_execution_graph(execute_decision_tool: Any) -> Any:
    """Compile the policy-gated execution graph around one `execute_decision` tool."""
    from langgraph.graph import END, START, StateGraph

    async def execute(state: ExecutionState) -> ExecutionState:
        try:
            raw = await execute_decision_tool.ainvoke(
                {"decision_id": state["decision_id"], "webhook_url": state["webhook_url"]}
            )
        except AlgentaExecutionBlocked as blocked:
            return {"outcome": "blocked", "gate": blocked.gate or "unknown"}
        receipt = parse_receipt(first_text_json(raw))
        if receipt is None:
            raise RuntimeError(f"execute_decision returned an unrecognized payload: {raw!r}")
        return {"outcome": "delivered" if receipt.is_delivered() else "failed"}

    async def accept(state: ExecutionState) -> ExecutionState:
        return {"verdict": "accepted"}

    async def quarantine(state: ExecutionState) -> ExecutionState:
        return {"verdict": "quarantined-delivery-failure"}

    async def review(state: ExecutionState) -> ExecutionState:
        return {"verdict": f"needs-review:{state['gate']}"}

    graph = StateGraph(ExecutionState)
    graph.add_node("execute", execute)
    graph.add_node("accept", accept)
    graph.add_node("quarantine", quarantine)
    graph.add_node("review", review)
    graph.add_edge(START, "execute")
    graph.add_conditional_edges(
        "execute",
        lambda state: state["outcome"],
        {"delivered": "accept", "failed": "quarantine", "blocked": "review"},
    )
    graph.add_edge("accept", END)
    graph.add_edge("quarantine", END)
    graph.add_edge("review", END)
    return graph.compile()


async def run_policy_gated_node(base_url: str, *, decision_id: str, webhook_url: str) -> ExecutionState:
    """Run one decision through the graph; return the final state (outcome, gate, verdict)."""
    tools = await create_algenta_tools(base_url=base_url, profile="execute")
    execute_decision = tool_by_name(tools, "execute_decision")
    app = build_execution_graph(execute_decision)
    return await app.ainvoke({"decision_id": decision_id, "webhook_url": webhook_url})


async def main() -> None:
    from tests.stub_server import FAILED_DELIVERY_DECISION_ID, LOW_CONFIDENCE_DECISION_ID

    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        for decision_id in ("decision-1", FAILED_DELIVERY_DECISION_ID, LOW_CONFIDENCE_DECISION_ID):
            state = await run_policy_gated_node(base_url, decision_id=decision_id, webhook_url="https://example.com/hook")
            print(f"{decision_id:>26} -> outcome={state['outcome']:<9} verdict={state['verdict']}")


if __name__ == "__main__":
    asyncio.run(main())
