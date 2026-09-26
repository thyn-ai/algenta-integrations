"""Audit-trailed chain: every LCEL chain run leaves an immutable decision-memory record.

LCEL (`prompt | model | parser`) is LangChain's core composition idiom, and tracing/audit
is the production requirement that always arrives next. This recipe bolts governance onto
any chain with a thin wrapper:

1. before the chain runs, `simulate` gives a governed pre-run estimate for the input,
2. the chain runs (a real LCEL `prompt | model | parser` pipeline),
3. after it completes, `log_decision` persists the run -- keyed on a deterministic
   SHA-256 fingerprint of `(question, output)` -- as an immutable decision-memory record
   whose `decision_id` is the audit receipt.

The wrapper is a plain `RunnableLambda`, so it composes with the rest of the LangChain
ecosystem: batch it, stream it, nest it in a larger graph. Same input always yields the
same fingerprint, so re-runs are detectable in the audit trail rather than silent.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.audit_trailed_chain
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from langchain_algenta import create_algenta_tools
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, first_text_json, tool_by_name

_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "You write one-line release-notes summaries."),
        ("human", "Summarize this change for the release notes: {question}"),
    ]
)


def run_fingerprint(question: str, output: str) -> str:
    """A deterministic content fingerprint of one chain run (input + output)."""
    return hashlib.sha256(f"{question}\n{output}".encode()).hexdigest()[:16]


async def run_audited_chain(base_url: str, question: str) -> dict[str, Any]:
    """Run the chain once with governance around it; return output plus audit receipt ids."""
    tools = await create_algenta_tools(base_url=base_url, profile="govern")
    simulate = tool_by_name(tools, "simulate")
    log_decision = tool_by_name(tools, "log_decision")

    async def _governed_run(question: str) -> dict[str, Any]:
        estimate = first_text_json(await simulate.ainvoke({"scenario": question}))

        # The inner chain is a real LCEL pipeline. `ScriptedChatModel` keeps the recipe
        # credential-free; the summary it "generates" is derived from the actual input so
        # the audit trail below reflects real content.
        model = ScriptedChatModel(script=[AIMessage(content=f"Summary of '{question}': governed and logged.")])
        chain = _PROMPT | model | StrOutputParser()
        output = await chain.ainvoke({"question": question})

        fingerprint = run_fingerprint(question, output)
        logged = first_text_json(await log_decision.ainvoke({"chosen_action": f"chain-run:{fingerprint}"}))
        return {
            "output": output,
            "fingerprint": fingerprint,
            "expected_value": estimate["expected_value"],
            "decision_id": logged["decision_id"],
            "logged_at": logged["created_at"],
        }

    audited_chain = RunnableLambda(_governed_run)
    return await audited_chain.ainvoke(question)


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        for question in ("ship the new pricing page", "deprecate the v1 webhook"):
            result = await run_audited_chain(base_url, question)
            print(f"{question!r} -> {result['decision_id']} (fingerprint {result['fingerprint']})")


if __name__ == "__main__":
    asyncio.run(main())
