"""Shared, non-fixture test helpers."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp.shared.exceptions import McpError
from temporal_algenta.activities import AlgentaActivities
from temporal_algenta.client import AlgentaMcpClient
from temporal_algenta.contract import ToolProfile
from temporal_algenta.types import SESSION_ERROR_TYPE
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

#: Bounded per-request timeout for every test session: a wedged session establishment (a
#: known, intermittent race in the MCP SDK's streamable-HTTP transport, surfacing as a stalled
#: response read) must fail fast so the retry wrappers below -- and Temporal's own retry
#: policy in workflow tests -- can heal it on a fresh session instead of stalling the suite
#: for the SDK's 60s default.
TEST_READ_TIMEOUT = timedelta(seconds=10)

#: How many times the resilient wrappers retry a session-level MCP failure. Retries here cover
#: exactly one failure class -- `McpError` from session establishment -- never governed-call
#: outcomes: policy denials, profile refusals, and unexpected payloads are never retried, so a
#: real regression still fails on the first attempt.
SESSION_ATTEMPTS = 3


@asynccontextmanager
async def workflow_worker(
    env: WorkflowEnvironment,
    workflows: Sequence[type],
    *,
    base_url: str,
    profile: ToolProfile,
    extra_activities: Sequence[Callable[..., Any]] = (),
) -> AsyncIterator[tuple[Client, str]]:
    """Run a `Worker` against the test's time-skipping environment for one test.

    Registers the test's workflow classes plus a fresh `AlgentaActivities` pointed at the
    test's own stub engine (per-test engine state isolation), on a unique task queue. The
    activities get `TEST_READ_TIMEOUT` so a wedged MCP session becomes a retryable activity
    error within the workflow's patience; the workflow's own retry policy then retries it
    transparently -- which is the durable-execution behavior being tested anyway.
    Yields `(client, task_queue)`.
    """
    algenta = AlgentaActivities(base_url=base_url, profile=profile, read_timeout=TEST_READ_TIMEOUT)
    task_queue = f"tq-{uuid.uuid4().hex}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=list(workflows),
        activities=[*algenta.all_activities(), *extra_activities],
    ):
        yield env.client, task_queue


async def with_session_resilient(
    *,
    base_url: str,
    profile: ToolProfile,
    fn: Callable[[AlgentaMcpClient], Awaitable[Any]],
) -> Any:
    """Run `fn(client)` over a fresh `AlgentaMcpClient` session, retrying only session-level
    `McpError`s (see `SESSION_ATTEMPTS`). Governed-call outcomes are returned or raised exactly
    as the single-attempt client produces them -- a policy denial, a profile refusal, or an
    unclassified tool error is never retried.

    `fn`'s exception is captured *inside* the session and re-raised only after the session has
    closed cleanly -- an exception crossing the MCP client's context-manager boundary trips a
    task-identity bug in the SDK's streamable-HTTP transport (spurious "cancel scope in a
    different task" / "async generator is already running" errors that mask the real failure).
    """
    for attempt in range(1, SESSION_ATTEMPTS + 1):
        captured: Exception | None = None
        async with AlgentaMcpClient(
            base_url=base_url, profile=profile, read_timeout=TEST_READ_TIMEOUT
        ) as client:
            try:
                return await fn(client)
            except Exception as error:
                captured = error
        if isinstance(captured, McpError):
            if attempt == SESSION_ATTEMPTS:
                raise captured
            continue
        assert captured is not None
        raise captured
    raise AssertionError("unreachable")


async def run_activity_resilient(
    activity_env: ActivityEnvironment, fn: Callable[..., Awaitable[Any]], *args: Any
) -> Any:
    """`ActivityEnvironment.run`, retrying only the retryable `SESSION_ERROR_TYPE` activity
    failure (the mapped session-level MCP failure -- see `SESSION_ATTEMPTS`). Everything else
    -- denials, profile refusals, unexpected payloads, and engine-reported tool errors
    (`TOOL_ERROR_TYPE`) -- propagates on the first attempt, so negative-path tests still see
    the raw single-attempt mapping.
    """
    for attempt in range(1, SESSION_ATTEMPTS + 1):
        try:
            return await activity_env.run(fn, *args)
        except ApplicationError as error:
            if error.type != SESSION_ERROR_TYPE or attempt == SESSION_ATTEMPTS:
                raise
    raise AssertionError("unreachable")
