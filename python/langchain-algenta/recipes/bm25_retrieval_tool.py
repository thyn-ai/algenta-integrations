"""BM25 retrieval as a LangChain tool, side by side with governed Algenta tools.

Lexical (BM25) retrieval tools are one of the most-used RAG building blocks on LangChain
-- every "search your docs" agent starts with one. This recipe shows the governed version
of that setup: a BM25 search tool and the engine's read-only `observe`-profile tools live
in the *same* tool list, the agent grounds its answer in both, and the application logs
the retrieval-grounded answer to decision memory for audit.

The BM25 index here is a small, dependency-free, fully deterministic Okapi BM25
implementation (stdlib only) so the recipe runs anywhere. Algenta's accelerated `bm25_mojo`
kernel is not published to PyPI, so recipes cannot depend on it; swap in your production
retriever (or the kernel, inside your own deployment) at the `BM25Index` seam -- the
tool-and-governance shape around it is unchanged.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.bm25_retrieval_tool
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any

from langchain_algenta import create_algenta_tools
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, first_text_json, tool_by_name


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Index:
    """Okapi BM25 over an in-memory corpus -- deterministic, dependency-free.

    Uses Robertson's non-negative idf (`ln(1 + (N - df + 0.5) / (df + 0.5))`) with the
    classic k1/b saturation parameters. Same input always produces the same ranking.
    """

    def __init__(self, documents: list[str], *, k1: float = 1.5, b: float = 0.75) -> None:
        if not documents:
            raise ValueError("BM25Index needs at least one document.")
        self._documents = documents
        self._k1 = k1
        self._b = b
        self._term_freqs: list[dict[str, int]] = []
        self._doc_freqs: dict[str, int] = {}
        for document in documents:
            counts: dict[str, int] = {}
            for token in _tokenize(document):
                counts[token] = counts.get(token, 0) + 1
            self._term_freqs.append(counts)
            for token in counts:
                self._doc_freqs[token] = self._doc_freqs.get(token, 0) + 1
        self._avg_len = sum(len(counts) for counts in self._term_freqs) / len(documents)

    def score(self, query: str, doc_index: int) -> float:
        """The BM25 score of one document for `query` (0.0 when nothing matches)."""
        doc_len = sum(self._term_freqs[doc_index].values())
        total = 0.0
        for token in _tokenize(query):
            tf = self._term_freqs[doc_index].get(token, 0)
            if tf == 0:
                continue
            df = self._doc_freqs[token]
            idf = math.log(1.0 + (len(self._documents) - df + 0.5) / (df + 0.5))
            norm = 1.0 - self._b + self._b * doc_len / self._avg_len
            total += idf * (tf * (self._k1 + 1.0)) / (tf + self._k1 * norm)
        return total

    def search(self, query: str, *, k: int = 3) -> list[tuple[str, float]]:
        """Return the top-`k` `(document, score)` pairs, best first; ties break by text."""
        ranked = sorted(
            ((document, self.score(query, index)) for index, document in enumerate(self._documents)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return [(document, score) for document, score in ranked[:k] if score > 0.0]


#: A tiny stand-in corpus for "your docs". Real deployments point the same tool at their
#: own index; the retrieval-and-governance shape is unchanged.
CORPUS: list[str] = [
    "Refund policy: a refund is issued within 30 days of the request once the return is received.",
    "The execute_decision tool is gated by idempotency, confidence, and risk floor policies.",
    "Decision memory records every logged decision immutably for later audit.",
    "Shipping to the EU takes between five and seven business days.",
]


def build_bm25_tool(index: BM25Index) -> BaseTool:
    """Wrap a `BM25Index` as a LangChain tool an agent can call."""

    async def _search(query: str) -> str:
        hits = index.search(query, k=2)
        if not hits:
            return "No matching documents."
        return "\n".join(f"- {document} (score {score:.3f})" for document, score in hits)

    return StructuredTool.from_function(
        name="local_bm25_search",
        description=(
            "Search the local document corpus with BM25 lexical ranking. "
            "Use this to ground answers in the corpus before responding."
        ),
        coroutine=_search,
    )


async def run_bm25_retrieval_tool(base_url: str, question: str) -> dict[str, Any]:
    """Run the mixed-toolset agent; return its answer, the top BM25 hit, and the audit id."""
    from langchain.agents import create_agent

    algenta_tools = await create_algenta_tools(base_url=base_url, profile="observe")
    index = BM25Index(CORPUS)
    tools = [build_bm25_tool(index), *algenta_tools]

    top_hit = index.search(question, k=1)[0][0]
    script = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "local_bm25_search", "args": {"query": question}, "id": "call_bm25", "type": "tool_call"}
            ],
        ),
        AIMessage(content=f"From the corpus: {top_hit}"),
    ]
    model = ScriptedChatModel(script=script)
    agent = create_agent(model, tools=tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})

    bm25_messages = [
        message for message in result["messages"] if isinstance(message, ToolMessage) and message.name == "local_bm25_search"
    ]
    if not bm25_messages:
        raise RuntimeError("The agent never called the BM25 tool.")

    govern_tools = await create_algenta_tools(base_url=base_url, profile="govern")
    log_decision = tool_by_name(govern_tools, "log_decision")
    logged = first_text_json(await log_decision.ainvoke({"chosen_action": "rag-bm25-answer"}))

    return {
        "final_message": result["messages"][-1].content,
        "top_hit": top_hit,
        "decision_id": logged["decision_id"],
    }


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_bm25_retrieval_tool(base_url, "refund policy")
        print("Top BM25 hit:", result["top_hit"])
        print("Agent:", result["final_message"])
        print("Audit receipt:", result["decision_id"])


if __name__ == "__main__":
    asyncio.run(main())
