from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from .stub_server import StubServerFixture


@pytest.fixture
async def stub_server() -> AsyncIterator[str]:
    """Run a fresh stub Algenta MCP server for the duration of one test; yield its base URL."""
    async with StubServerFixture() as server:
        yield server.base_url
