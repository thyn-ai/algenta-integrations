"""Tests for `recipes/governed_rag.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

from recipes.governed_rag import run_governed_rag


async def test_governed_rag_returns_a_grounded_answer_with_an_audit_receipt(stub_server: str) -> None:
    result = await run_governed_rag(stub_server, "How are Algenta decisions audited?")

    assert result["decision_id"].startswith("decision-rag-answer:")
    assert result["sources"], "expected at least one retrieved source"
    assert result["governed_rows"] == 2  # the stub engine's query_data fixture returns two rows
    assert "governed rows" in result["answer"]


async def test_governed_rag_retrieval_is_relevant_to_the_question(stub_server: str) -> None:
    result = await run_governed_rag(stub_server, "How are Algenta decisions audited?")

    # DeterministicFakeEmbedding makes retrieval reproducible; the audit-trail passage is
    # the closest match for this question in the recipe's knowledge base.
    assert any("decision-memory" in source for source in result["sources"])
