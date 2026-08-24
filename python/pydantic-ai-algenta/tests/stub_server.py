"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `fastmcp.FastMCP` server, run over a real HTTP socket on `127.0.0.1` -- not an
in-memory transport shortcut and not a mock of `AlgentaToolset`'s internals. Tests exercise the
real `AlgentaToolset` -> real `pydantic_ai.mcp.MCPToolset` -> real `fastmcp.Client` -> real wire
-> this server round trip, so a wire-shape regression (e.g. the receipt envelope not surviving
structured-content round-tripping) would actually be caught here, unlike a test that mocks
`call_tool` directly.

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
Every tool below is a hand-built fake shaped like the real, documented decision lifecycle:
`plan_decision` / `log_decision` return their own freeform results, and `execute_decision` is
the one tool with a real typed envelope -- a successful `ExecutionReceipt` (HTTP 200) or a named
policy-gate `ExecutionDenial` (HTTP 409), fully synchronously, never a "pending" third state.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from fastmcp import FastMCP

#: A `decision_id` whose `execute_decision` call always comes back blocked by the real
#: `"confidence"` gate, unless `override_safety=True`.
LOW_CONFIDENCE_DECISION_ID = "decision-low-confidence"

#: A `decision_id` whose `execute_decision` call always comes back blocked by the real
#: `"risk_floor"` gate, unless `override_safety=True`.
RISK_FLOOR_DECISION_ID = "decision-risk-floor-breach"

#: A tool whose result is intentionally *not* an `execute_decision`-shaped envelope, to exercise
#: the passthrough path for tools that don't return one (e.g. a real `get_contract` discovery
#: blob, or `plan_decision`'s freeform summary).
NON_ENVELOPE_RESULT = {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}


def _denial(*, gate: str, message: str, override_hint: str) -> dict[str, Any]:
    """Build a dict shaped exactly like the real synchronous 409 denial body."""
    return {"error": {"code": f"execution_blocked_{gate}", "gate": gate, "message": message, "override_hint": override_hint}}


def _receipt(*, decision_id: str, webhook_url: str, execution_status: str, safety_overridden: bool) -> dict[str, Any]:
    """Build a dict shaped exactly like the real successful `ExecutionReceipt`."""
    return {
        "decision_id": decision_id,
        "webhook_url": webhook_url,
        "execution_status": execution_status,
        "response_code": 200 if execution_status == "delivered" else 502,
        "executed_at": "2026-08-23T00:00:00Z",
        "policy_snapshot_id": "policy-snap-1",
        "schema_snapshot_id": "schema-snap-1",
        "manifest_version": "1.0.0",
        "payload_summary": {"decision_id": decision_id},
        "safety_overridden": safety_overridden,
    }


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated per-decision delivery state."""
    mcp = FastMCP("algenta-stub")
    delivered_decision_ids: set[str] = set()

    @mcp.tool
    def get_contract() -> dict:
        """Fake discovery payload -- deliberately not an `execute_decision`-shaped envelope."""
        return dict(NON_ENVELOPE_RESULT)

    @mcp.tool
    def query_data(dataset: str) -> dict:
        return {"dataset": dataset, "rows": [{"value": 1}, {"value": 2}]}

    @mcp.tool
    def simulate(scenario: str) -> dict:
        return {"scenario": scenario, "expected_value": 42.0}

    @mcp.tool
    def recommend(scenario: str) -> dict:
        return {"scenario": scenario, "recommended_action": "hold", "confidence": 0.87}

    @mcp.tool
    def plan_decision(scenario: str) -> dict:
        return {"summary": f"proposed plan for {scenario}", "scenario": scenario}

    @mcp.tool
    def log_decision(chosen_action: str) -> dict:
        return {
            "decision_id": f"dec-{chosen_action}",
            "chosen_action": chosen_action,
            "expected_value": 42.0,
            "confidence": 0.91,
            "created_at": "2026-08-23T00:00:00Z",
            "note": None,
        }

    @mcp.tool
    def execute_decision(
        decision_id: str,
        webhook_url: str,
        timeout_seconds: int = 30,
        force: bool = False,
        override_safety: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict:
        """The real, safety-critical, fully-synchronous tool.

        `force` / `override_safety` are declared on this fake tool's schema on purpose,
        mirroring the real contract's note that `execute_decision` carries these two
        operator-only fields on its real schema -- `AlgentaToolset` is the thing under test for
        stripping them, not this server.
        """
        if decision_id == LOW_CONFIDENCE_DECISION_ID and not override_safety:
            return _denial(
                gate="confidence",
                message="decision confidence is below policy.min_confidence",
                override_hint="Set override_safety=true to bypass the confidence gate.",
            )
        if decision_id == RISK_FLOOR_DECISION_ID and not override_safety:
            return _denial(
                gate="risk_floor",
                message="risk_p5 is below -policy.risk_floor",
                override_hint="Set override_safety=true to bypass the risk_floor gate.",
            )
        if decision_id in delivered_decision_ids and not force:
            return _denial(
                gate="idempotency",
                message=f"decision {decision_id!r} has already been delivered",
                override_hint="Set force=true to override the idempotency gate for one re-execution.",
            )
        delivered_decision_ids.add(decision_id)
        return _receipt(
            decision_id=decision_id,
            webhook_url=webhook_url,
            execution_status="delivered",
            safety_overridden=force or override_safety,
        )

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


class StubServerFixture:
    """Async context manager that runs `build_stub_algenta_server()` over a real HTTP socket."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self.base_url: str = ""

    async def __aenter__(self) -> StubServerFixture:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, port = sock.getsockname()
        self._sock = sock

        mcp = build_stub_algenta_server()
        self._task = asyncio.create_task(mcp.run_http_async(show_banner=False, path="/mcp", sockets=[sock]))
        await _wait_until_serving(port)
        self.base_url = f"http://127.0.0.1:{port}/mcp"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        if self._sock is not None:
            self._sock.close()
