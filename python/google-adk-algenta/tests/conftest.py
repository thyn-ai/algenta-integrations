from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from .stub_server import StubServerFixture


@pytest.fixture
def anyio_backend() -> str:
    # Pin to asyncio only -- without this, anyio's plugin parametrizes every `anyio`-marked test
    # over every backend `anyio.get_available_backends()` finds importable, which would include
    # trio if it's ever pulled in transitively. This package has no trio dependency and no
    # reason to test against it.
    return "asyncio"


@pytest.fixture
async def stub_server() -> AsyncIterator[str]:
    """Run a fresh stub Algenta MCP server for the duration of one test; yield its base URL."""
    async with StubServerFixture() as server:
        yield server.base_url
