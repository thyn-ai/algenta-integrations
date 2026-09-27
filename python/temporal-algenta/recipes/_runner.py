"""Shared machinery for running a recipe end-to-end with zero credentials.

Every recipe's `main()` builds on `recipe_worker(...)`, which wires up the three things any
recipe run needs:

1. **An Algenta Engine to govern against.** With `ALGENTA_BASE_URL` set, your own self-hosted
   engine. Without it, a deterministic in-process demo engine (`recipes/demo_engine.py`)
   served on an ephemeral `127.0.0.1` port -- so recipes run with no credentials at all.
2. **A Temporal server.** With `TEMPORAL_ADDRESS` set (or `ALGENTA_BASE_URL` implying a real
   setup), your own server/namespace via `Client.connect`. Without it, the SDK's local
   time-skipping test server (`WorkflowEnvironment.start_time_skipping()`), downloaded and
   started on the fly -- no separate Temporal install needed.
3. **A `Worker`** registered with the recipe's workflows and a fresh `AlgentaActivities` whose
   `base_url`/`profile` match the chosen engine.

This module is imported inside recipe `main()` bodies only -- never at recipe module level --
so Temporal's workflow sandbox never has to load its transitive dependencies (`mcp`, `uvicorn`,
the Temporal client) when validating workflow code.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from temporal_algenta.activities import AlgentaActivities
from temporal_algenta.client import ALGENTA_BASE_URL_ENV_VAR
from temporal_algenta.contract import ToolProfile
from temporalio.client import Client
from temporalio.worker import Worker

#: Environment variables a recipe run honors when pointing at real infrastructure.
TEMPORAL_ADDRESS_ENV_VAR = "TEMPORAL_ADDRESS"
TEMPORAL_NAMESPACE_ENV_VAR = "TEMPORAL_NAMESPACE"
DEFAULT_TEMPORAL_ADDRESS = "localhost:7233"
DEFAULT_TEMPORAL_NAMESPACE = "default"


@asynccontextmanager
async def recipe_runtime(*, profile: ToolProfile) -> AsyncIterator[tuple[str, Client]]:
    """Resolve the engine endpoint + Temporal client for one recipe run.

    Yields `(base_url, client)`. See the module docstring for the two modes (demo vs. real
    infrastructure) and the environment variables that select between them.
    """
    from temporalio.testing import WorkflowEnvironment

    from .demo_engine import serve_demo_engine

    base_url = os.environ.get(ALGENTA_BASE_URL_ENV_VAR)
    if base_url:
        client = await Client.connect(
            os.environ.get(TEMPORAL_ADDRESS_ENV_VAR, DEFAULT_TEMPORAL_ADDRESS),
            namespace=os.environ.get(TEMPORAL_NAMESPACE_ENV_VAR, DEFAULT_TEMPORAL_NAMESPACE),
        )
        yield base_url, client
        return

    async with serve_demo_engine() as (demo_base_url, _engine):
        async with await WorkflowEnvironment.start_time_skipping() as env:
            yield demo_base_url, env.client


@asynccontextmanager
async def recipe_worker(
    workflows: Sequence[type],
    *,
    profile: ToolProfile,
    task_queue: str | None = None,
    extra_activities: Sequence[Callable[..., Any]] = (),
) -> AsyncIterator[tuple[Client, str]]:
    """Run a `Worker` for one recipe execution; yields `(client, task_queue)`.

    The worker is registered with the recipe's workflow classes plus one fresh
    `AlgentaActivities(base_url=<resolved>, profile=profile)` -- see the module docstring.
    `extra_activities` lets a recipe register its own domain activities (e.g. a compensation
    step) alongside the Algenta ones.
    """
    async with recipe_runtime(profile=profile) as (base_url, client):
        algenta = AlgentaActivities(base_url=base_url, profile=profile)
        queue = task_queue or f"algenta-recipe-{uuid.uuid4().hex[:8]}"
        async with Worker(
            client,
            task_queue=queue,
            workflows=list(workflows),
            activities=[*algenta.all_activities(), *extra_activities],
        ):
            yield client, queue


__all__ = [
    "DEFAULT_TEMPORAL_ADDRESS",
    "DEFAULT_TEMPORAL_NAMESPACE",
    "TEMPORAL_ADDRESS_ENV_VAR",
    "TEMPORAL_NAMESPACE_ENV_VAR",
    "recipe_runtime",
    "recipe_worker",
]
