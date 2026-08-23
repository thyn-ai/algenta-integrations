from __future__ import annotations

from collections.abc import Iterator

import pytest

from .stub_server import StubServerFixture


@pytest.fixture
def stub_server() -> Iterator[str]:
    """Run a fresh stub Algenta MCP server for the duration of one test; yield its base URL.

    A plain synchronous fixture -- no `pytest-asyncio` anywhere in this package's test suite,
    because `MCPToolset`/`Agent`'s real, public API is itself synchronous (see `stub_server.py`'s
    module docstring). This is a genuine framework-level difference from this package's four
    Python siblings, not an oversight.
    """
    with StubServerFixture() as server:
        yield server.base_url
