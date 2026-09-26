"""Governed RAG: retrieval-augmented generation with a governed data path and an audit receipt per answer.

RAG is LangChain's flagship pattern -- retrieve context, stuff it into a prompt, generate.
This recipe shows the governed version of that shape:

1. retrieve supporting passages from an in-memory vector store (any retriever works --
   swap in your own),
2. ground the answer in *governed* data via the engine's `query_data` tool (the
   `observe`-profile, read-only path every Algenta integration exposes),
3. generate the answer through a real LCEL chain (`prompt | model | parser`),
4. persist the answer as an immutable decision-memory record via `log_decision`, so every
   generated answer carries a `decision_id` audit receipt you can later query, score, or
   close the outcome loop on.

The model here is `ScriptedChatModel` -- the chain shape, retrieval, and tool calls are
entirely real; only the weights are stubbed so the recipe runs with zero credentials.

Run it (from `algenta-integrations/python/langchain-algenta`):

    uv run python -m recipes.governed_rag
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from langchain_algenta import create_algenta_tools
from langchain_core.documents import Document
from langchain_core.embeddings.fake import DeterministicFakeEmbedding
from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.vectorstores import InMemoryVectorStore

from ._stub import serve_recipe_stub
from ._support import ScriptedChatModel, first_text_json, tool_by_name

#: The recipe's tiny knowledge base. `DeterministicFakeEmbedding` makes retrieval fully
#: reproducible; with a real embedding model the same code runs unchanged.
KNOWLEDGE_BASE: list[Document] = [
    Document(
        page_content="Algenta decision memory keeps an immutable, hash-chained audit trail of every logged decision.",
        metadata={"source": "docs/decision-memory.md"},
    ),
    Document(
        page_content="The execute_decision tool either returns a receipt or is blocked by one of three named policy gates.",
        metadata={"source": "docs/execution.md"},
    ),
    Document(
        page_content="The observe tool profile is read-only: get_contract, query_data, simulate, and recommend.",
        metadata={"source": "docs/profiles.md"},
    ),
]

_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "Answer the question using only the supplied context passages and governed data rows."),
        ("human", "Question: {question}\n\nContext passages:\n{context}\n\nGoverned rows: {rows}"),
    ]
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


async def run_governed_rag(base_url: str, question: str) -> dict[str, Any]:
    """Run one governed RAG pass; return the answer plus its decision-memory receipt ids."""
    tools = await create_algenta_tools(base_url=base_url, profile="govern")
    query_data = tool_by_name(tools, "query_data")
    log_decision = tool_by_name(tools, "log_decision")

    store = InMemoryVectorStore(embedding=DeterministicFakeEmbedding(size=32))
    await store.aadd_documents(KNOWLEDGE_BASE)
    passages = await store.asimilarity_search(question, k=2)

    governed = first_text_json(await query_data.ainvoke({"dataset": "policy_handbook"}))
    rows = governed["rows"]

    context = "\n".join(f"- {doc.page_content}" for doc in passages)
    draft = (
        f"Based on {len(passages)} passages and {len(rows)} governed rows: "
        f"{passages[0].page_content} "
        f"(Governed dataset {governed['dataset']!r} contributed {len(rows)} rows.)"
    )
    model = ScriptedChatModel(script=[AIMessage(content=draft)])
    chain = _PROMPT | model | StrOutputParser()
    answer = await chain.ainvoke({"question": question, "context": context, "rows": rows})

    logged = first_text_json(await log_decision.ainvoke({"chosen_action": f"rag-answer:{_slug(question)}"}))

    return {
        "answer": answer,
        "sources": [doc.metadata["source"] for doc in passages],
        "governed_rows": len(rows),
        "decision_id": logged["decision_id"],
        "logged_at": logged["created_at"],
    }


async def main() -> None:
    async with serve_recipe_stub() as base_url:
        print(f"Stub Algenta MCP server listening at {base_url}")
        result = await run_governed_rag(base_url, "How are Algenta decisions audited?")
        print("Answer:", result["answer"])
        print("Sources:", result["sources"])
        print(f"Audit receipt: decision_id={result['decision_id']} logged_at={result['logged_at']}")


if __name__ == "__main__":
    asyncio.run(main())
