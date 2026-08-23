"""Shared, non-fixture test helpers."""

from __future__ import annotations

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage


def bare_run_context() -> RunContext[None]:
    """A minimal, standalone `RunContext` for calling `AlgentaToolset.get_tools`/`call_tool`
    directly, without spinning up a full `Agent.run(...)`.

    Not backed by a real run -- fine for the toolset-level unit tests in this suite, which care
    about what `AlgentaToolset` does with the context it's given, not about run-level bookkeeping
    (usage limits, message history, ...).
    """
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())
