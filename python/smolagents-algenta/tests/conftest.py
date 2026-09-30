from __future__ import annotations

from collections.abc import Iterator

import pytest

from .stub_server import StubServerFixture


@pytest.fixture
def stub_server() -> Iterator[str]:
    """Run a fresh stub Algenta MCP server for the duration of one test; yield its base URL."""
    with StubServerFixture() as server:
        yield server.base_url
