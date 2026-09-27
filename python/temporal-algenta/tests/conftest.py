from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from recipes.demo_engine import DemoAlgentaEngine
from temporalio.testing import WorkflowEnvironment

from .stub_server import StubServerFixture


@pytest.fixture
async def stub_server() -> AsyncIterator[tuple[str, DemoAlgentaEngine]]:
    """Run a fresh stub Algenta engine for the duration of one test (unit tests' loop scope).

    Yields `(base_url, engine)`: the URL for `AlgentaActivities(base_url=...)` and the engine
    itself for asserting on delivered ids, logged decisions, and per-tool attempt counts.
    """
    async with StubServerFixture() as server:
        yield server.base_url, server.engine


@pytest_asyncio.fixture(loop_scope="module")
async def stub_server_mod() -> AsyncIterator[tuple[str, DemoAlgentaEngine]]:
    """The same fresh-per-test stub engine, but running on the *module* loop so recipe tests
    (which use the module-scoped Temporal environments below) can use it. Function-scoped:
    every test still gets its own isolated engine state.
    """
    async with StubServerFixture() as server:
        yield server.base_url, server.engine


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
