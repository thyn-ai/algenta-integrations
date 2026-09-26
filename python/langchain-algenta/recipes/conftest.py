"""pytest fixtures for the recipe tests: stub Algenta MCP servers, zero credentials.

Mirrors the package suite's own `tests/conftest.py`, but serving `recipes._stub`'s
optionally-extended server builds so the decision-memory and scoring recipes get the real
registry surface they exercise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ._stub import serve_recipe_stub


@pytest.fixture
async def stub_server() -> AsyncIterator[str]:
    """The base stub Algenta MCP server (the four contract profiles' named tools)."""
    async with serve_recipe_stub() as base_url:
        yield base_url


@pytest.fixture
async def memory_stub_server() -> AsyncIterator[str]:
    """The base stub plus the real registry's Decision Memory reads (full-profile only)."""
    async with serve_recipe_stub(decision_memory=True) as base_url:
        yield base_url


@pytest.fixture
async def score_stub_server() -> AsyncIterator[str]:
    """The base stub plus the real registry's deterministic `score` tool (full-profile only)."""
    async with serve_recipe_stub(score=True) as base_url:
        yield base_url
