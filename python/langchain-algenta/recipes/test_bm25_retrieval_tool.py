"""Tests for `recipes/bm25_retrieval_tool.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

from recipes.bm25_retrieval_tool import CORPUS, BM25Index, run_bm25_retrieval_tool


def test_bm25_ranks_the_matching_document_first() -> None:
    index = BM25Index(CORPUS)
    hits = index.search("refund policy", k=1)
    assert hits, "expected at least one hit"
    assert hits[0][0].startswith("Refund policy:")


def test_bm25_is_deterministic_and_zero_for_no_match() -> None:
    index = BM25Index(CORPUS)
    assert index.search("refund policy", k=2) == index.search("refund policy", k=2)
    assert index.search("zzz-not-in-corpus", k=2) == []


async def test_the_agent_grounds_its_answer_in_the_bm25_hit_and_logs_it(stub_server: str) -> None:
    result = await run_bm25_retrieval_tool(stub_server, "refund policy")

    assert result["top_hit"].startswith("Refund policy:")
    assert result["top_hit"] in result["final_message"]
    assert result["decision_id"] == "decision-rag-bm25-answer"
