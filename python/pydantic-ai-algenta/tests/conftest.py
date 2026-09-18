from __future__ import annotations

from collections.abc import AsyncIterator

import pydantic_ai.models
import pytest

from .stub_server import StubServerFixture

# A real, enforced guard against a test accidentally instantiating a real model class and
# making a live network call: real `Model` subclasses check this flag inside `request()` /
# `request_stream()` and raise if it's `False`. `TestModel` (used throughout this suite) is a
# test double and doesn't check it, so this has no effect on the tests below beyond the guard
# itself. See pydantic-ai's own testing docs for the convention this mirrors.
pydantic_ai.models.ALLOW_MODEL_REQUESTS = False


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
