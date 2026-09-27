"""Tests for `recipes/bm25_retrieval_tool.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

import pytest

from recipes import bm25_retrieval_tool as recipe
from recipes.bm25_retrieval_tool import (
    CORPUS,
    KERNEL_SOURCE,
    BM25Index,
    naive_search,
    run_bm25_retrieval_tool,
)


def test_bm25_ranks_the_matching_document_first() -> None:
    index = BM25Index(CORPUS)
    hits = index.search("refund policy", k=1)
    assert hits, "expected at least one hit"
    assert hits[0][0].startswith("Refund policy:")


def test_bm25_is_deterministic_and_zero_for_no_match() -> None:
    index = BM25Index(CORPUS)
    assert index.search("refund policy", k=2) == index.search("refund policy", k=2)
    assert index.search("zzz-not-in-corpus", k=2) == []


def test_kernel_source_names_the_active_engine() -> None:
    assert KERNEL_SOURCE in {"bm25_mojo", "python-standin"}
    assert (recipe._bm25_mojo is None) == (KERNEL_SOURCE == "python-standin")


def test_kernel_and_python_paths_rank_identically_when_kernel_is_installed() -> None:
    """The kernel is a speed upgrade, never a correctness one: with `bm25_mojo` installed, the
    kernel path must produce the pure-Python path's exact ranking (scores may differ by float
    noise; the order may not)."""
    if KERNEL_SOURCE != "bm25_mojo":
        pytest.skip("bm25-mojo not installed -- parity is exercised where the kernel is available")
    with_kernel = BM25Index(CORPUS)
    forced_python = BM25Index(CORPUS)
    forced_python._kernel = None  # test-only seam: force the pure-Python path
    for query in ("refund policy", "decision memory audit", "shipping", "execute_decision gates"):
        kernel_ranking = [document for document, _ in with_kernel.search(query, k=len(CORPUS))]
        python_ranking = [document for document, _ in forced_python.search(query, k=len(CORPUS))]
        assert kernel_ranking == python_ranking


def test_naive_baseline_is_deterministic_and_finds_the_relevant_document() -> None:
    first = naive_search(CORPUS, "refund policy", k=2)
    assert naive_search(CORPUS, "refund policy", k=2) == first
    assert first and first[0][0].startswith("Refund policy:")
    assert naive_search(CORPUS, "zzz-not-in-corpus") == []


async def test_the_agent_grounds_its_answer_in_the_bm25_hit_and_logs_it(stub_server: str) -> None:
    result = await run_bm25_retrieval_tool(stub_server, "refund policy")

    assert result["top_hit"].startswith("Refund policy:")
    assert result["top_hit"] in result["final_message"]
    assert result["decision_id"] == "decision-rag-bm25-answer"
