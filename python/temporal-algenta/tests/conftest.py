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
    """One time-skipping `WorkflowEnvironment` per test FILE.

    Starting the local test server spawns a subprocess; doing it per test (the previous
    arrangement) measurably degraded the process for later tests in a long suite -- after
    enough server lifecycles, every subsequent start stalled on both macOS and Linux CI
    runners (reproduced in CI logs: the first ~10 env-backed tests fly, then every further one
    times out). One env per file keeps the lifecycle count flat while tests stay isolated via
    unique task queues and workflow ids (see tests/helpers.py). Time skipping means durable
    timers (the approval timeout in `recipes/human_approval_workflow.py`) fire without real
    waiting. Startup and shutdown are bounded so a wedged lifecycle surfaces as a loud error,
    never a silent hang.
    """
    env = await asyncio.wait_for(WorkflowEnvironment.start_time_skipping(), timeout=90)
    try:
        yield env
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def temporal_env_realtime() -> AsyncIterator[WorkflowEnvironment]:
    """One real-time `WorkflowEnvironment` (the SDK's local dev server) per test FILE.

    Needed for signal-driven tests whose workflow parks on a *long, armed* durable timer: on a
    time-skipping server the skip fires that timer the instant the workflow goes idle, so an
    operator signal sent afterwards always arrives "too late" (observed directly: the query
    races the skip, the workflow times out first, and the query RPC then hangs). A real-time
    server leaves the hour-long approval deadline alone while the test signals in real
    seconds. Durable-deadline behavior itself is still covered on the time-skipping
    environment (see `test_approval_timeout_abandons_the_execution`). Also required for the
    schedule recipe test: the time-skipping test server does not implement the schedule RPCs
    ("CreateSchedule is unimplemented"). Module-scoped for the same lifecycle reason as
    `temporal_env`.
    """
    env = await asyncio.wait_for(WorkflowEnvironment.start_local(), timeout=90)
    try:
        yield env
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)
