"""Conformance-suite adapter tests for langchain-algenta.

The suite in `demo/conformance` is written against an abstract `Adapter` protocol. These tests prove
that the LangChain adapter satisfies that protocol and that all 9 exercisable scenarios pass through
it against a real (but fake, local) HTTP engine. The 3 blocked scenarios are still reported as
blocked -- a blocked scenario is not a pass.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

# The adapter lives under the repo-root `demo/` tree, not under this package, so the repo root must
# be on sys.path for the duration of the test. This mirrors the local quickstart pattern shown in
# the package README.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from demo.conformance.adapters.langchain import LangChainAdapter  # noqa: E402
from demo.conformance.runner import Engine, run  # noqa: E402
from demo.conformance.tests.stub_engine import StubEngineFixture  # noqa: E402


@pytest.fixture
async def stub_engine_url() -> Any:
    """Yield the base URL of a freshly started stub conformance engine."""
    async with StubEngineFixture() as fixture:
        yield fixture.base_url


async def test_langchain_adapter_runs_full_suite(stub_engine_url: str) -> None:
    adapter = LangChainAdapter(stub_engine_url, "test-key")
    # `run()` is synchronous because the real CLI is synchronous. Run it in a worker thread so the
    # synchronous HTTP calls do not block the event loop that the stub engine is running on.
    results = await asyncio.to_thread(run, adapter)

    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    blocked = sum(1 for r in results if r.passed is None)

    assert failed == 0, [r for r in results if r.passed is False]
    assert blocked == 3, blocked
    assert passed == 9, passed


async def test_direct_adapter_runs_full_suite(stub_engine_url: str) -> None:
    """Sanity-check the direct stdlib adapter against the same stub engine.

    This guards against the stub engine drifting out of sync with the scenario expectations:
    if the direct adapter (the existing baseline) fails against the stub, the stub is the bug.
    """
    adapter = Engine(stub_engine_url, "test-key")
    results = await asyncio.to_thread(run, adapter)

    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    blocked = sum(1 for r in results if r.passed is None)

    assert failed == 0, [r for r in results if r.passed is False]
    assert blocked == 3, blocked
    assert passed == 9, passed


async def test_langchain_adapter_is_runtime_adapter(stub_engine_url: str) -> None:
    """The LangChain adapter actually implements the Adapter protocol at runtime."""
    from demo.conformance.adapters import Adapter

    adapter = LangChainAdapter(stub_engine_url, "test-key")
    assert isinstance(adapter, Adapter)
