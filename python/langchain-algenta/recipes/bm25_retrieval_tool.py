"""BM25 retrieval as a LangChain tool, side by side with governed Algenta tools.

Lexical (BM25) retrieval tools are one of the most-used RAG building blocks on LangChain
-- every "search your docs" agent starts with one. This recipe shows the governed version
of that setup: a BM25 search tool and the engine's read-only `observe`-profile tools live
in the *same* tool list, the agent grounds its answer in both, and the application logs
the retrieval-grounded answer to decision memory for audit.

The retrieval hot path has two interchangeable BM25 engines behind the one `BM25Index` seam:

- **`bm25_mojo`** (preferred): Algenta's published Mojo kernel (`pip install bm25-mojo`, live
  on PyPI since 0.1.5), used transparently when importable. It is an *optional, undeclared*
  runtime extra on purpose: this repository's own CI gate (`scripts/check-no-engine-dependency.py`)
  forbids any manifest dependency on the kernel tooling, so the kernel can only ever be a
  runtime-optional import here, never a `pyproject.toml` dependency.
- **A deterministic pure-Python Okapi BM25 stand-in** (stdlib only, always available): the same
  scoring formula (Robertson idf, k1=1.5, b=0.75), so the same corpus produces the same ranking
  on either path. The kernel is a *speed* upgrade, never a *correctness* one.

`main()` prints the measured speed of the BM25 path against a naive token-overlap recount
(same corpus, same run, same hardware), and names the engine that produced the numbers.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.bm25_retrieval_tool
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any, Final

from langchain_algenta import create_algenta_tools
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, first_text_json, tool_by_name

try:  # Optional acceleration kernel -- see the module docstring for why it's undeclared.
    import bm25_mojo as _bm25_mojo
except ImportError:
    _bm25_mojo = None

#: Which BM25 engine `BM25Index` is actually using in this process -- `main()` prints this in
#: its speed note so the numbers are attributable.
KERNEL_SOURCE: Final = "bm25_mojo" if _bm25_mojo is not None else "python-standin"

#: rank_bm25's epsilon for the idf floor: negative idfs are replaced by
#: `_IDF_EPSILON x average_idf` (the kernel's exact semantics -- see `_ensure_python_index`).
_IDF_EPSILON: Final = 0.25


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Index:
    """Okapi BM25 over an in-memory corpus, on the `bm25_mojo` kernel when importable and on a
    deterministic pure-Python path otherwise. Both engines implement the kernel's
    rank_bm25-compatible formula exactly (plain Robertson idf with the 0.25 x average-idf
    epsilon floor, k1=1.5, b=0.75), so the same corpus produces the same ranking on either
    path -- the kernel is a speed upgrade, never a correctness one.
    """

    def __init__(self, documents: list[str], *, k1: float = 1.5, b: float = 0.75) -> None:
        if not documents:
            raise ValueError("BM25Index needs at least one document.")
        self._documents = documents
        self._k1 = k1
        self._b = b
        self._kernel = (
            _bm25_mojo.BM25Okapi([_tokenize(document) for document in documents])
            if _bm25_mojo is not None
            else None
        )
        # The pure-Python inverted index is built lazily on first use: when the kernel is
        # active it is never needed, and building it anyway would charge the kernel path a
        # second, redundant indexing pass per construction.
        self._term_freqs: list[dict[str, int]] | None = None
        self._doc_freqs: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._avg_len = 0.0

    def _ensure_python_index(self) -> None:
        """Build the pure-Python inverted index on first use (the only path that needs it).

        The idf table mirrors the `bm25_mojo` kernel's rank_bm25-compatible semantics exactly:
        plain Robertson idf `ln((N - df + 0.5) / (df + 0.5))`, with negative values replaced by
        `0.25 * average_idf` (rank_bm25's epsilon floor) -- so the two engines produce the same
        ranking for the same corpus by construction, and the kernel is a pure speed upgrade.
        """
        if self._term_freqs is not None:
            return
        term_freqs: list[dict[str, int]] = []
        doc_freqs: dict[str, int] = {}
        for document in self._documents:
            counts: dict[str, int] = {}
            for token in _tokenize(document):
                counts[token] = counts.get(token, 0) + 1
            term_freqs.append(counts)
            for token in counts:
                doc_freqs[token] = doc_freqs.get(token, 0) + 1
        doc_count = len(self._documents)
        raw_idf = {
            term: math.log((doc_count - df + 0.5) / (df + 0.5)) for term, df in doc_freqs.items()
        }
        average_idf = sum(raw_idf.values()) / len(raw_idf) if raw_idf else 0.0
        self._idf = {
            term: value if value >= 0.0 else _IDF_EPSILON * average_idf
            for term, value in raw_idf.items()
        }
        self._term_freqs = term_freqs
        self._doc_freqs = doc_freqs
        self._avg_len = sum(len(counts) for counts in term_freqs) / doc_count

    def _scores(self, query: str) -> list[float]:
        """One BM25 score per document, on whichever engine is active."""
        if self._kernel is not None:
            return [float(score) for score in self._kernel.get_scores(_tokenize(query))]
        return [self._score_python(query, index) for index in range(len(self._documents))]

    def _score_python(self, query: str, doc_index: int) -> float:
        """The pure-Python path: the BM25 score of one document for `query`."""
        self._ensure_python_index()
        assert self._term_freqs is not None  # narrowed by _ensure_python_index
        doc_len = sum(self._term_freqs[doc_index].values())
        total = 0.0
        for token in _tokenize(query):
            tf = self._term_freqs[doc_index].get(token, 0)
            if tf == 0:
                continue
            norm = 1.0 - self._b + self._b * doc_len / self._avg_len
            total += self._idf[token] * (tf * (self._k1 + 1.0)) / (tf + self._k1 * norm)
        return total

    def score(self, query: str, doc_index: int) -> float:
        """The BM25 score of one document for `query` (0.0 when nothing matches)."""
        return self._scores(query)[doc_index]

    def search(self, query: str, *, k: int = 3) -> list[tuple[str, float]]:
        """Return the top-`k` `(document, score)` pairs, best first; ties break by text."""
        ranked = sorted(
            enumerate(self._scores(query)),
            key=lambda pair: (-pair[1], self._documents[pair[0]]),
        )
        return [(self._documents[index], score) for index, score in ranked[:k] if score > 0.0]


def naive_search(documents: list[str], query: str, *, k: int = 3) -> list[tuple[str, float]]:
    """The un-accelerated baseline: naive per-document token-overlap recount (no IDF, the query
    re-tokenized for every document, O(docs x query_terms x doc_terms) counting). Exists so
    `main()` can measure the BM25 path against the approach a first implementation would take
    -- same corpus, same run, same hardware. Not for production use.
    """
    scored: list[tuple[str, float]] = []
    for document in documents:
        hits = 0.0
        for query_token in _tokenize(query):
            for doc_token in _tokenize(document):
                if query_token == doc_token:
                    hits += 1.0
        scored.append((document, hits))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [(document, score) for document, score in scored[:k] if score > 0.0]


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
                {
                    "name": "local_bm25_search",
                    "args": {"query": question},
                    "id": "call_bm25",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(content=f"From the corpus: {top_hit}"),
    ]
    model = ScriptedChatModel(script=script)
    agent = create_agent(model, tools=tools)
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})

    bm25_messages = [
        message
        for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == "local_bm25_search"
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

    # The retrieval hot path, measured the way the tool actually runs in production: the index
    # is built ONCE, then many queries hit it. (A cold rebuild-per-query measurement penalizes
    # an indexed engine for its one-time build and is not how retrieval is deployed.) Only the
    # real kernel gets the measured comparison -- see below.
    if KERNEL_SOURCE == "bm25_mojo":
        documents = CORPUS * 2000  # 8000 docs
        queries = [
            "refund policy",
            "decision memory audit",
            "execute_decision gates",
            "shipping eu",
            "idempotency confidence",
            "logged decision",
            "risk floor",
            "business days",
        ]
        reps = 25  # 25 passes x 8 queries = 200 measured queries per engine
        # One-time builds (printed for amortization transparency, excluded from query timing).
        started = time.perf_counter()
        kernel_index = BM25Index(documents)
        kernel_build_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        standin_index = BM25Index(documents)
        standin_index._kernel = None  # force the pure-Python engine (demo seam)
        standin_index._ensure_python_index()
        standin_build_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        for _ in range(reps):
            for q in queries:
                kernel_index.search(q, k=2)
        kernel_ms = (time.perf_counter() - started) / (reps * len(queries)) * 1000
        started = time.perf_counter()
        for _ in range(reps):
            for q in queries:
                standin_index.search(q, k=2)
        standin_ms = (time.perf_counter() - started) / (reps * len(queries)) * 1000
        started = time.perf_counter()
        for _ in range(reps):
            for q in queries:
                naive_search(documents, q, k=2)
        naive_ms = (time.perf_counter() - started) / (reps * len(queries)) * 1000

        print(
            f"\nRetrieval hot path ({len(documents)} docs, 200 measured queries), live, same run:"
        )
        print(
            f"  one-time index build: bm25_mojo {kernel_build_ms:.0f} ms vs python {standin_build_ms:.0f} ms"
        )
        print(f"  per query, bm25_mojo:    {kernel_ms:.3f} ms")
        print(
            f"  per query, python bm25:  {standin_ms:.3f} ms ({standin_ms / kernel_ms:.1f}x slower)"
        )
        print(f"  per query, naive recount: {naive_ms:.3f} ms ({naive_ms / kernel_ms:.1f}x slower)")
    else:
        print(
            f"\nRetrieval hot path: running the deterministic pure-Python stand-in ({KERNEL_SOURCE})."
        )
        print(
            "  pip install bm25-mojo for the Mojo-accelerated kernel and re-run for live numbers."
        )


if __name__ == "__main__":
    asyncio.run(main())
