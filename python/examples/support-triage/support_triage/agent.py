"""LangGraph state machine for the governed support-triage queue.

The graph walks every ticket through the same arc the issue asks for:

  observe -> govern -> approve -> execute -> persist

- **observe**: the agent may only use read-only tools (`recommend`).
- **govern**: the agent may only use planning/recording tools (`plan_decision`,
  `log_decision`); `execute_decision` is not in either tool list.
- **approve**: a caller-supplied gate decides whether the logged decision may run.
- **execute**: application code, not the model, holds the `execute`-profile tool.
- **persist**: the resulting `ExecutionReceipt` is written to local disk.

All engine calls go through the real `langchain-algenta` MCP client round trip,
exactly as production code would. The local demo substitutes a deterministic stub
server for a self-hosted engine; tests use the same stub.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypedDict

from langchain_algenta import (
    AlgentaExecutionBlocked,
    ExecutionReceipt,
    create_algenta_tools,
    parse_receipt,
)
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from support_triage.store import ReceiptStore

ApprovalGate = Callable[[dict[str, Any]], Awaitable[bool]]
"""A gate that decides whether a logged decision may be executed."""


class TriageState(TypedDict, total=False):
    """Mutable graph state for one pass through the triage queue."""

    tickets: list[dict[str, Any]]
    current_index: int
    current_ticket: dict[str, Any] | None
    observation: dict[str, Any] | None
    decision_record: dict[str, Any] | None
    approved: bool | None
    receipt: ExecutionReceipt | None
    block_reason: str | None
    persisted_paths: list[str]
    done: bool


def _tool_by_name(tools: list[BaseTool], name: str) -> BaseTool:
    """Return the one tool named `name`, or fail loudly with what exists."""
    for tool in tools:
        if tool.name == name:
            return tool
    available = sorted(tool.name for tool in tools)
    raise KeyError(f"No tool named {name!r}; available tools: {available}")


def _first_text_json(raw_result: Any) -> Any:
    """Unwrap the JSON payload from a LangChain MCP tool result.

    Real MCP tool returns a list of content blocks; the tool's JSON payload is the
    first text block's text.
    """
    if isinstance(raw_result, list):
        for block in raw_result:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block["text"])
    raise AssertionError(f"expected a list of text content blocks, got {raw_result!r}")


def _webhook_url_for_ticket(ticket: dict[str, Any]) -> str:
    """Resolve a deterministic webhook URL for a ticket.

    The example does not make real outbound network calls; the webhook URL is
    part of the decision receipt shape and is resolved from the ticket's own
    `webhook_url` field, falling back to a safe, local placeholder.
    """
    return ticket.get("webhook_url", "http://localhost:8000/webhook")


def _chosen_action_for_ticket(ticket: dict[str, Any]) -> str:
    """Resolve the action the govern step will log for a ticket."""
    return ticket.get("action") or ticket.get("suggested_action") or "hold"


def _scenario_for_ticket(ticket: dict[str, Any]) -> str:
    """Resolve the scenario text sent to observe/govern tools."""
    return ticket.get("scenario") or ticket.get("subject") or ""


def build_triage_graph(
    *,
    base_url: str,
    approval_gate: ApprovalGate,
    receipts_dir: str | Path,
) -> Any:
    """Compile the governed support-triage LangGraph.

    Args:
        base_url: The self-hosted Algenta MCP endpoint to talk to. The local demo
            starts a stub server on 127.0.0.1 and passes its URL here.
        approval_gate: An async callable that receives the logged decision record
            and returns ``True`` if execution should proceed.
        receipts_dir: Directory where successful execution receipts are persisted.

    Returns:
        A compiled LangGraph ready to be invoked with a ``TriageState``.
    """
    store = ReceiptStore(receipts_dir)

    async def load_ticket(state: TriageState) -> dict[str, Any]:
        """Advance the queue, or mark the run done when no tickets remain."""
        index = state.get("current_index", 0)
        tickets = state.get("tickets", [])
        if index >= len(tickets):
            return {"done": True}

        return {
            "current_index": index + 1,
            "current_ticket": tickets[index],
            "observation": None,
            "decision_record": None,
            "approved": None,
            "receipt": None,
            "block_reason": None,
            "done": False,
        }

    async def observe(state: TriageState) -> dict[str, Any]:
        """Call read-only tools to observe the current ticket."""
        ticket = state["current_ticket"]
        assert ticket is not None
        tools = await create_algenta_tools(base_url=base_url, profile="observe")
        recommend = _tool_by_name(tools, "recommend")
        raw = await recommend.ainvoke({"scenario": _scenario_for_ticket(ticket)})
        return {"observation": _first_text_json(raw)}

    async def govern(state: TriageState) -> dict[str, Any]:
        """Plan and log a decision for the current ticket using govern-profile tools."""
        ticket = state["current_ticket"]
        assert ticket is not None
        tools = await create_algenta_tools(base_url=base_url, profile="govern")
        plan_decision = _tool_by_name(tools, "plan_decision")
        log_decision = _tool_by_name(tools, "log_decision")

        scenario = _scenario_for_ticket(ticket)
        chosen_action = _chosen_action_for_ticket(ticket)

        # Plan first, then log the chosen action. Both calls are real MCP round trips.
        await plan_decision.ainvoke({"scenario": scenario})
        raw = await log_decision.ainvoke({"chosen_action": chosen_action})
        return {"decision_record": _first_text_json(raw)}

    async def approve(state: TriageState) -> dict[str, Any]:
        """Ask the caller-supplied approval gate whether execution may proceed."""
        decision_record = state["decision_record"]
        assert decision_record is not None
        approved = await approval_gate(decision_record)
        return {"approved": approved}

    async def execute(state: TriageState) -> dict[str, Any]:
        """Execute the approved decision using the execute-profile tool handle."""
        decision_record = state["decision_record"]
        ticket = state["current_ticket"]
        assert decision_record is not None and ticket is not None

        execute_tools = await create_algenta_tools(base_url=base_url, profile="execute")
        execute_decision = _tool_by_name(execute_tools, "execute_decision")

        try:
            raw = await execute_decision.ainvoke(
                {
                    "decision_id": decision_record["decision_id"],
                    "webhook_url": _webhook_url_for_ticket(ticket),
                }
            )
        except AlgentaExecutionBlocked as blocked:
            return {
                "receipt": None,
                "block_reason": f"blocked:{blocked.gate} -- {blocked.denial.message}",
            }

        receipt = parse_receipt(_first_text_json(raw))
        if receipt is None:
            raise RuntimeError(f"execute_decision returned an unrecognized payload: {raw!r}")
        return {"receipt": receipt, "block_reason": None}

    async def persist(state: TriageState) -> dict[str, Any]:
        """Write a successful execution receipt to the configured store."""
        receipt = state.get("receipt")
        if receipt is None:
            return {}
        path = store.persist(receipt)
        return {"persisted_paths": state.get("persisted_paths", []) + [str(path)]}

    async def skip(state: TriageState) -> dict[str, Any]:  # noqa: ARG001
        """No-op node for tickets that were not approved."""
        return {}

    graph = StateGraph(TriageState)
    graph.add_node("load_ticket", load_ticket)
    graph.add_node("observe", observe)
    graph.add_node("govern", govern)
    graph.add_node("approve", approve)
    graph.add_node("execute", execute)
    graph.add_node("persist", persist)
    graph.add_node("skip", skip)

    graph.add_edge(START, "load_ticket")
    graph.add_conditional_edges(
        "load_ticket",
        lambda state: "done" if state.get("done") else "observe",
        {"done": END, "observe": "observe"},
    )
    graph.add_edge("observe", "govern")
    graph.add_edge("govern", "approve")
    graph.add_conditional_edges(
        "approve",
        lambda state: "execute" if state.get("approved") else "skip",
        {"execute": "execute", "skip": "skip"},
    )
    graph.add_edge("execute", "persist")
    graph.add_edge("persist", "load_ticket")
    graph.add_edge("skip", "load_ticket")

    return graph.compile()


async def run_triage(
    base_url: str,
    tickets: list[dict[str, Any]],
    approval_gate: ApprovalGate,
    receipts_dir: str | Path,
) -> TriageState:
    """Run the governed triage queue and return the final graph state.

    Args:
        base_url: Self-hosted Algenta MCP endpoint (or stub URL for local demo/tests).
        tickets: Queue of support tickets to process. Each ticket should have at
            least a ``scenario``/``subject`` and an ``action``/``suggested_action``.
        approval_gate: Injected human-in-the-loop gate.
        receipts_dir: Directory where successful receipts are written.

    Returns:
        The final ``TriageState``, including ``persisted_paths`` for every ticket
        that was approved and executed successfully.
    """
    app = build_triage_graph(
        base_url=base_url, approval_gate=approval_gate, receipts_dir=receipts_dir
    )
    return await app.ainvoke(
        {"tickets": tickets, "current_index": 0, "persisted_paths": []},
        {"recursion_limit": max(20, len(tickets) * 10)},
    )
