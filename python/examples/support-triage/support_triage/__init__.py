"""Governed support-triage example app on LangGraph.

`support_triage.run_triage` drives a LangGraph state machine through the
observe -> govern -> approve -> execute -> persist arc for a queue of support
tickets, using a self-hosted Algenta engine (or the local zero-setup stub server)
and a caller-supplied approval gate.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from support_triage.agent import TriageState, build_triage_graph, run_triage
from support_triage.store import ReceiptStore

try:
    __version__ = version("support-triage")
except PackageNotFoundError:  # pragma: no cover - only fails when run unpackaged.
    __version__ = "0.0.0+unknown"

__all__ = [
    "ReceiptStore",
    "TriageState",
    "build_triage_graph",
    "run_triage",
]
