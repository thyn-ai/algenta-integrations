"""Stub Algenta MCP servers for the recipes -- real local sockets, zero credentials.

Every recipe runs against the same kind of deterministic, fake self-hosted Algenta MCP
server the package's own test suite uses (`tests/stub_server.py`): a real
`mcp.server.fastmcp.FastMCP` server over a real HTTP socket on 127.0.0.1 -- never a mock of
the MCP wire, and never a real engine (none is reachable from a laptop or CI).

`build_recipe_server` starts from the package suite's own stub server builder and can add
more of the real engine's documented MCP registry surface for the recipes that need it:

- `decision_memory=True` adds the registry's Decision Memory reads -- `list_decisions`,
  `get_decision`, `record_outcome` -- with the same tool names and result shapes a real
  self-hosted engine advertises (the write side, `log_decision`, is one of the four
  contract profiles' named tools and comes from the base stub).
- `score=True` adds the registry's `score` tool -- a deterministic composite of a
  simulation request's expected value and probability of loss, exactly the shape the real
  registry documents (`recommended_action`, `expected_value`, `probability_of_loss`,
  `score`, `score_breakdown`).

These extra tools sit outside the four contract tool profiles' named sets, so -- exactly
like on a real engine -- they are only reachable through the opt-in `full` profile. The
recipes that use them do so from application code (an eval harness, decision-memory
bookkeeping), which is precisely the admin/ops use the `full` profile exists for -- never
as a model-facing default.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from tests.stub_server import build_stub_algenta_server

#: The base stub's `log_decision` reports this constant `expected_value` for every freshly
#: logged decision (and mints decision ids as `decision-<chosen_action>`). The
#: decision-memory extension needs both facts to close the feedback loop on decision ids
#: the base stub created: `outcome_delta = actual_outcome - expected_value`.
BASE_LOGGED_EXPECTED_VALUE = 42.0

#: Deterministic seeded decision-memory history, so `list_decisions` has something real to
#: page over before any new `record_outcome` call -- mirroring an engine that has been
#: logging decisions for a while. `outcome_delta` is `actual_outcome - expected_value`:
#: negative means the decision turned out worse than predicted.
_SEEDED_DECISIONS: list[dict[str, Any]] = [
    {
        "decision_id": "decision-expand-eu-q1",
        "chosen_action": "expand-eu",
        "expected_value": 58.0,
        "confidence": 0.84,
        "context": "Q1 EU expansion review",
        "actual_outcome": 61.0,
        "outcome_delta": 3.0,
        "created_at": "2026-03-31T00:00:00Z",
        "outcome_recorded_at": "2026-06-30T00:00:00Z",
    },
    {
        "decision_id": "decision-raise-prices-q2",
        "chosen_action": "raise-prices",
        "expected_value": 74.0,
        "confidence": 0.79,
        "context": "Q2 pricing review",
        "actual_outcome": 66.5,
        "outcome_delta": -7.5,
        "created_at": "2026-06-30T00:00:00Z",
        "outcome_recorded_at": "2026-08-31T00:00:00Z",
    },
    {
        "decision_id": "decision-hold-q3",
        "chosen_action": "hold",
        "expected_value": 35.0,
        "confidence": 0.92,
        "context": "Q3 planning",
        "actual_outcome": None,
        "outcome_delta": None,
        "created_at": "2026-09-01T00:00:00Z",
        "outcome_recorded_at": None,
    },
]


def _add_decision_memory_tools(mcp: FastMCP) -> None:
    """Add the real registry's Decision Memory reads to a base-stub server instance.

    Deterministic, in-memory stand-ins for a live engine's persisted decision memory:
    seeded history plus whatever `record_outcome` has recorded on this server instance.
    Recording converges on repeat calls with the same value, matching the real tool's
    documented idempotency.
    """
    recorded: dict[str, dict[str, Any]] = {}

    def _full_record(entry: dict[str, Any]) -> dict[str, Any]:
        return dict(entry)

    def _seeded_ids() -> set[str]:
        return {entry["decision_id"] for entry in _SEEDED_DECISIONS}

    def _seeded_decisions() -> list[dict[str, Any]]:
        # Seeded entries a later `record_outcome` call has closed the loop on show the
        # recorded values -- the stub's stand-in for the real tool's in-place update.
        return [recorded.get(entry["decision_id"], entry) for entry in _SEEDED_DECISIONS]

    @mcp.tool()
    def list_decisions(with_outcome_only: bool = False, page: int = 1, limit: int = 20) -> dict:
        """Decision Memory audit trail, most recent first, with an accuracy summary.

        Mirrors the real registry tool's shape: paged `decisions` (each with
        `decision_id`, `chosen_action`, `expected_value`, `actual_outcome`,
        `outcome_delta`, `confidence`, `created_at`, `outcome_recorded_at`) plus `total`,
        `page`, `limit`, `pages`, and an `accuracy_summary` when outcomes exist.
        """
        entries = [*_seeded_decisions(), *(recorded[did] for did in recorded if did not in _seeded_ids())]
        entries.sort(key=lambda entry: entry["created_at"], reverse=True)
        if with_outcome_only:
            entries = [entry for entry in entries if entry["actual_outcome"] is not None]
        total = len(entries)
        start = max(page - 1, 0) * limit
        page_entries = entries[start : start + limit]
        with_outcome = [entry for entry in entries if entry["actual_outcome"] is not None]
        accuracy_summary = None
        if with_outcome:
            mean_delta = round(sum(entry["outcome_delta"] for entry in with_outcome) / len(with_outcome), 2)
            accuracy_summary = {"with_outcome": len(with_outcome), "mean_outcome_delta": mean_delta}
        return {
            "decisions": [_full_record(entry) for entry in page_entries],
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, (total + limit - 1) // limit),
            "accuracy_summary": accuracy_summary,
        }

    @mcp.tool()
    def get_decision(decision_id: str) -> dict:
        """Fetch one decision-memory record by id, or a tool error if it doesn't exist."""
        record = recorded.get(decision_id) or next(
            (entry for entry in _SEEDED_DECISIONS if entry["decision_id"] == decision_id), None
        )
        if record is None:
            raise RuntimeError(json.dumps({"error": {"code": "not_found", "message": f"No decision {decision_id!r}."}}))
        return _full_record(record)

    @mcp.tool()
    def record_outcome(decision_id: str, actual_outcome: float, outcome_notes: str | None = None) -> dict:
        """Close the feedback loop on one logged decision: record what actually happened.

        Returns the real tool's shape: `decision_id`, `chosen_action`, `expected_value`,
        `actual_outcome`, `outcome_delta`, and a human-readable `summary`. Repeat calls
        with the same value converge (the real tool updates the record in place).
        """
        seeded = next((entry for entry in _SEEDED_DECISIONS if entry["decision_id"] == decision_id), None)
        if seeded is not None:
            chosen_action = seeded["chosen_action"]
            expected_value = seeded["expected_value"]
        else:
            # A decision id the base stub's `log_decision` minted (`decision-<action>`):
            # its result contract fixes expected_value at BASE_LOGGED_EXPECTED_VALUE.
            chosen_action = decision_id.removeprefix("decision-")
            expected_value = BASE_LOGGED_EXPECTED_VALUE
        outcome_delta = round(actual_outcome - expected_value, 2)
        recorded[decision_id] = {
            "decision_id": decision_id,
            "chosen_action": chosen_action,
            "expected_value": expected_value,
            "confidence": None,
            "context": outcome_notes,
            "actual_outcome": actual_outcome,
            "outcome_delta": outcome_delta,
            "created_at": "2026-09-26T00:00:00Z",
            "outcome_recorded_at": "2026-09-26T00:00:00Z",
        }
        return {
            "decision_id": decision_id,
            "chosen_action": chosen_action,
            "expected_value": expected_value,
            "actual_outcome": actual_outcome,
            "outcome_delta": outcome_delta,
            "summary": f"Outcome {actual_outcome} recorded for {decision_id} (delta {outcome_delta}).",
        }


def _add_score_tool(mcp: FastMCP) -> None:
    """Add the real registry's `score` tool to a base-stub server instance.

    The real tool runs one simulation request and blends the normalized expected value
    with one minus the probability of loss (`scoring_weights` tunes the blend;
    expected_value default 0.6, downside_risk default 0.4). This stand-in derives both
    inputs deterministically from a SHA-256 of the request payload, so scores are stable
    across runs and genuinely input-dependent -- the two properties the eval recipe
    exercises -- without any simulation backend.
    """

    @mcp.tool()
    def score(request: dict, scoring_weights: dict | None = None) -> dict:
        """Deterministic composite score for one simulation-style request payload."""
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        expected_value = round((int(digest[:8], 16) % 10_000) / 100.0, 2)
        probability_of_loss = round((int(digest[8:16], 16) % 100) / 100.0, 2)
        weights = scoring_weights or {}
        weight_ev = weights.get("expected_value", 0.6)
        weight_risk = weights.get("downside_risk", 0.4)
        composite = round(weight_ev * (expected_value / 100.0) + weight_risk * (1.0 - probability_of_loss), 4)
        return {
            "recommended_action": request.get("candidate", "unknown"),
            "expected_value": expected_value,
            "probability_of_loss": probability_of_loss,
            "score": composite,
            "score_breakdown": {
                "expected_value_weight": weight_ev,
                "downside_risk_weight": weight_risk,
                "normalized_expected_value": round(expected_value / 100.0, 4),
                "one_minus_probability_of_loss": round(1.0 - probability_of_loss, 4),
            },
        }


def build_recipe_server(*, decision_memory: bool = False, score: bool = False) -> FastMCP:
    """Build the package suite's stub Algenta MCP server, optionally extended.

    The base builder is the exact one `tests/stub_server.py`'s fixture serves -- the four
    contract profiles' named tools with the real `execute_decision` three-gate denial
    model. Extensions add real registry surface outside those named sets, reachable (as on
    a real engine) only via the opt-in `full` profile.
    """
    mcp = build_stub_algenta_server()
    if decision_memory:
        _add_decision_memory_tools(mcp)
    if score:
        _add_score_tool(mcp)
    return mcp


async def _wait_until_serving(port: int, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_event_loop().time() > deadline:
                raise
            await asyncio.sleep(0.02)
            continue
        writer.close()
        await writer.wait_closed()
        return


@asynccontextmanager
async def serve_recipe_stub(*, decision_memory: bool = False, score: bool = False) -> AsyncIterator[str]:
    """Serve a recipe stub server over a real HTTP socket on 127.0.0.1; yield its base URL.

    Same serving mechanics as `tests/stub_server.py`'s `StubServerFixture` (manually bound
    ephemeral socket + `uvicorn.Server(...).serve(sockets=[sock])`), parameterized by the
    server instance so recipes can serve an extended registry. Every test/recipe gets its
    own isolated server on its own free port.
    """
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    _, port = sock.getsockname()

    mcp = build_recipe_server(decision_memory=decision_memory, score=score)
    app = mcp.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _wait_until_serving(port)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        sock.close()
