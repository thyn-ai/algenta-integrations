"""Shared, non-fixture test helpers."""

from __future__ import annotations

from unittest.mock import MagicMock


def bare_tool_context() -> MagicMock:
    """A minimal, standalone `ToolContext` stand-in for calling tool methods directly.

    Not backed by a real ADK run -- fine for the toolset-level unit tests in this suite, which
    care about what `AlgentaToolset` does with the context it's given, not about run-level
    bookkeeping.
    """
    return MagicMock()
