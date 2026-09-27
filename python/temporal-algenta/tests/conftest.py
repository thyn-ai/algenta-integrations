from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from recipes.demo_engine import DemoAlgentaEngine
from temporalio.testing import WorkflowEnvironment

from .stub_server import StubServerFixture


@pytest.fixture
async def stub_server() -> AsyncIterator[tuple[str, DemoAlgentaEngine]]:
    """Run a fresh stub Algenta engine for the duration of one test.

    Yields `(base_url, engine)`: the URL for `AlgentaActivities(base_url=...)` and the engine
    itself for asserting on delivered ids, logged decisions, and per-tool attempt counts.
    """
    async with StubServerFixture() as server:
        yield server.base_url, server.engine


@pytest.fixture
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    """One time-skipping `WorkflowEnvironment` per test.

    Starting the local test server takes a second or two; a per-test server (rather than a
    session-scoped one) keeps every test on its own event loop with zero shared state -- see
    the `tool.pytest.ini_options` comment in this package's pyproject.toml for the
    shared-session-loop hang this avoids. Time skipping means durable timers (the approval
    timeout in `recipes/human_approval_workflow.py`) fire without real waiting.

    Both startup and shutdown are bounded: a wedged ephemeral-server lifecycle must surface as
    a loud test error, never a silent suite hang.
    """
    env = await asyncio.wait_for(WorkflowEnvironment.start_time_skipping(), timeout=90)
    try:
        yield env
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)


@pytest.fixture
async def temporal_env_realtime() -> AsyncIterator[WorkflowEnvironment]:
    """One real-time `WorkflowEnvironment` (the SDK's local dev server) per test.

    Needed for signal-driven tests whose workflow parks on a *long, armed* durable timer: on a
    time-skipping server the skip fires that timer the instant the workflow goes idle, so an
    operator signal sent afterwards always arrives "too late" (observed directly: the query
    races the skip, the workflow times out first, and the query RPC then hangs). A real-time
    server leaves the hour-long approval deadline alone while the test signals in real
    seconds. Durable-deadline behavior itself is still covered on the time-skipping
    environment (see `test_approval_timeout_abandons_the_execution`). Startup/shutdown bounded
    like `temporal_env`.
    """
    env = await asyncio.wait_for(WorkflowEnvironment.start_local(), timeout=90)
    try:
        yield env
    finally:
        await asyncio.wait_for(env.shutdown(), timeout=30)
