from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from recipes.demo_engine import DemoAlgentaEngine
from temporalio.testing import WorkflowEnvironment

from .stub_server import ThreadedEngineServer


@pytest.fixture
def stub_server() -> Iterator[tuple[str, DemoAlgentaEngine]]:
    """Run a fresh stub Algenta engine for the duration of one test, on its own thread and
    event loop (see `stub_server_mod` for why an in-loop server is not safe in this suite --
    the same intermittent wedge applies to unit tests, observed in a CI rerun where
    test_client/test_activities stalled identically to the recipe tests).

    Yields `(base_url, engine)`: the URL for `AlgentaActivities(base_url=...)` and the engine
    itself for asserting on delivered ids, logged decisions, and per-tool attempt counts.
    """
    server = ThreadedEngineServer().start()
    try:
        yield server.base_url, server.engine
    finally:
        server.stop()


@pytest.fixture(scope="module")
def stub_server_mod() -> Iterator[tuple[str, DemoAlgentaEngine]]:
    """ONE stub engine server per test FILE, on its own thread and event loop.

    An in-loop server is not safe here: this suite runs several sequential servers per pytest
    process, and each test file's loop also hosts a Temporal dev-server client and per-test
    workers. On a shared loop a slow graceful shutdown forces `serve_demo_engine`'s cancel
    fallback, stranding FastMCP's lifespan half-shut -- the NEXT server then accepts
    connections but never completes responses (reproduced in CI and in local Linux containers
    as `initialize()` read timeouts plus uvicorn's "ASGI callable returned without completing
    response"; the first recipe test passes, every later one stalls). A dedicated thread per
    server removes the shared-loop mechanism entirely -- see `tests/stub_server.py`.

    Tests get their isolation from `engine_state` below, which resets the engine's plain-data
    state before each test -- exactly equivalent to a fresh engine, since the tools read state
    at call time. Being a plain sync fixture, this also carries no pytest-asyncio loop-scope
    constraints at all.
    """
    server = ThreadedEngineServer().start()
    try:
        yield server.base_url, server.engine
    finally:
        server.stop()


@pytest.fixture
def engine_state(stub_server_mod: tuple[str, DemoAlgentaEngine]) -> tuple[str, DemoAlgentaEngine]:
    """Per-test pristine engine state against the module's running stub server."""
    base_url, engine = stub_server_mod
    engine.reset()
    return base_url, engine


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """One real-time `WorkflowEnvironment` (the SDK's local dev server) per test FILE.

    Real-time, not time-skipping, on purpose: the time-skipping test server fast-forwards
    durable timers the instant a workflow goes idle, which is exactly what a workflow waiting
    on an activity (every recipe workflow) is doing -- under load, the skip intermittently
    beats the activity's completion and the activity's own start_to_close timeout fires
    instead (reproduced in CI logs as workflows that stall until the test's bound fires). A
    real-time server has no skip semantics to race: activities take their real milliseconds.
    One env per file (module scope) keeps server lifecycles flat while tests stay isolated via
    unique task queues and workflow ids (see tests/helpers.py). Startup and shutdown are
    bounded so a wedged lifecycle surfaces as a loud error, never a silent hang.
    """
    env = await asyncio.wait_for(WorkflowEnvironment.start_local(), timeout=90)
    try:
        yield env
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def temporal_env_skipping() -> AsyncIterator[WorkflowEnvironment]:
    """One time-skipping `WorkflowEnvironment` per test FILE, for the one test that needs
    time fast-forwarded: `test_approval_timeout_abandons_the_execution` (a one-second durable
    approval deadline, fired instantly by the skip instead of a real wait). Nothing else
    should use this -- see `temporal_env` for why.
    """
    env = await asyncio.wait_for(WorkflowEnvironment.start_time_skipping(), timeout=90)
    try:
        yield env
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)
