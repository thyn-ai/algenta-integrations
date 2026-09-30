"""pytest fixtures for the support-triage example tests: zero credentials, real stub server."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from support_triage._stub import StubServerFixture


@pytest.fixture
async def stub_server() -> AsyncIterator[str]:
    """Run a fresh stub Algenta MCP server; yield its base URL."""
    async with StubServerFixture() as server:
        yield server.base_url
