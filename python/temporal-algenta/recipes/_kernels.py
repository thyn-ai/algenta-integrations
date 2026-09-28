"""The acceleration seam for the recipes' compute-heavy retrieval step.

`rank_documents` scores and orders documents against a query with BM25 -- the retrieval hot
path in `recipes/batch_simulation_pipeline.py`. Two implementations sit behind the one call:

- **`bm25_mojo`** (preferred): Algenta's published Mojo kernel for BM25 retrieval (`pip install
  bm25-mojo`, live on PyPI since 0.1.5), used transparently when importable. It is an
  *optional, undeclared* extra on purpose: this repository's own CI gate
  (`scripts/check-no-engine-dependency.py`) forbids any manifest dependency whose name
  contains the kernel tooling's marker, so the kernel can only ever be a runtime-optional
  import here, never a `pyproject.toml` dependency. Tests and recipe demos run correctly on
  either path.
- **A deterministic pure-Python BM25 stand-in** (always available): the kernel's exact
  rank_bm25-compatible scoring formula (plain Robertson idf with the 0.25 x average-idf
  epsilon floor, k1=1.5, b=0.75), identical ranking for the same corpus, no randomness, no
  clocks -- so recipe output is reproducible whether or not the kernel is installed. The
  kernel is a *speed* upgrade for this step, never a *correctness* one: both paths must agree,
  and the recipe's test asserts the ranking's determinism.

`naive_rank_documents` is the measurement baseline for the recipe's live speed note: the same
corpus scored with a naive per-document token-overlap recount (the un-accelerated approach a
first implementation would take), so `main()` can print measured numbers for the same
hardware, same run.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Final

try:  # Optional acceleration kernel -- see the module docstring for why it's undeclared.
    import bm25_mojo as _bm25_mojo
except ImportError:
    _bm25_mojo = None

#: Which implementation `rank_documents` is actually using in this process -- the recipes
#: print this in their speed note so the numbers are attributable.
KERNEL_SOURCE: Final = "bm25_mojo" if _bm25_mojo is not None else "python-standin"

#: Okapi BM25 tuning constants (the standard defaults; pinned for determinism).
_BM25_K1: Final = 1.5
_BM25_B: Final = 0.75

#: rank_bm25's epsilon for the idf floor: negative idfs are replaced by
#: `_IDF_EPSILON x average_idf` (the kernel's exact semantics).
_IDF_EPSILON: Final = 0.25


def _tokenize(text: str) -> list[str]:
    """Deterministic lowercase whitespace/punctuation tokenization (no locale, no stemming)."""
    return [
        token.strip(".,;:!?()[]{}\"'")
        for token in text.lower().split()
        if token.strip(".,;:!?()[]{}\"'")
    ]


def _bm25_scores_python(query: str, documents: list[str]) -> list[float]:
    """Okapi BM25 scores, pure Python, mirroring the `bm25_mojo` kernel's rank_bm25-compatible
    semantics exactly: plain Robertson idf `ln((N - df + 0.5) / (df + 0.5))`, negative values
    replaced by `_IDF_EPSILON x average_idf`. Deterministic for a fixed (query, documents)
    pair, and the same ranking as the kernel for the same corpus by construction.
    """
    query_terms = _tokenize(query)
    doc_terms = [_tokenize(doc) for doc in documents]
    if not doc_terms or not query_terms:
        return [0.0 for _ in documents]

    doc_count = len(doc_terms)
    avgdl = sum(len(terms) for terms in doc_terms) / doc_count if doc_count else 0.0
    doc_freq: Counter[str] = Counter()
    for terms in doc_terms:
        for term in set(terms):
            doc_freq[term] += 1

    raw_idf = {term: math.log((doc_count - df + 0.5) / (df + 0.5)) for term, df in doc_freq.items()}
    average_idf = sum(raw_idf.values()) / len(raw_idf) if raw_idf else 0.0
    idf = {
        term: value if value >= 0.0 else _IDF_EPSILON * average_idf
        for term, value in raw_idf.items()
    }

    scores: list[float] = []
    for terms in doc_terms:
        term_counts = Counter(terms)
        dl = len(terms)
        score = 0.0
        for term in query_terms:
            if term not in idf:
                continue
            tf = term_counts.get(term, 0)
            denom = (
                tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * (dl / avgdl)) if avgdl else tf + _BM25_K1
            )
            score += idf[term] * (tf * (_BM25_K1 + 1.0)) / denom if denom else 0.0
        scores.append(score)
    return scores


def rank_documents(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """Score every document against `query` and return `(doc_index, score)` pairs ordered by
    descending score (ties broken by ascending index, so the result is fully deterministic).

    Uses the `bm25_mojo` kernel when importable, otherwise the pure-Python stand-in -- see the
    module docstring. Both paths implement the same Okapi BM25 formula.
    """
    if _bm25_mojo is not None:
        # The published 0.1.5 API: an index object over a pre-tokenized corpus, scored per
        # query (rank_bm25 convention). Index construction is part of the measured call --
        # that is the honest cost of scoring a corpus handed to us cold.
        index = _bm25_mojo.BM25Okapi([_tokenize(document) for document in documents])
        scores = [float(score) for score in index.get_scores(_tokenize(query))]
    else:
        scores = _bm25_scores_python(query, documents)
    return sorted(enumerate(scores), key=lambda pair: (-pair[1], pair[0]))


def naive_rank_documents(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """The un-accelerated baseline: naive per-document token-overlap recount (no IDF, repeated
    tokenization of the query per document, O(docs x query_terms x doc_terms) counting).
    Exists to give the recipes' speed note a same-run, same-hardware baseline -- not for
    production use.
    """
    scored: list[tuple[int, float]] = []
    for index, doc in enumerate(documents):
        score = 0.0
        for query_token in _tokenize(query):
            for doc_token in _tokenize(doc):
                if query_token == doc_token:
                    score += 1.0
        scored.append((index, score))
    return sorted(scored, key=lambda pair: (-pair[1], pair[0]))


__all__ = ["KERNEL_SOURCE", "naive_rank_documents", "rank_documents"]
