"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `fastmcp.FastMCP` server, run over a real HTTP socket on `127.0.0.1` -- not an
in-memory transport shortcut and not a mock of `llamaindex_algenta`'s internals. Tests exercise
the real `create_algenta_tools` -> real `llama_index.tools.mcp.BasicMCPClient` -> real wire ->
this server round trip, so a wire-shape regression (e.g. the receipt envelope not surviving
`structuredContent` round-tripping) would actually be caught here, unlike a test that mocks
`call_tool` directly.

Nothing here talks to any real Algenta engine -- none is reachable from this test environment.
Every tool below is a hand-built fake shaped like the real, documented tool contract (see
`llamaindex_algenta.receipts` for `execute_decision`'s two real shapes -- `ExecutionReceipt` on
success, `ExecutionDenial` on a named-gate block), plus one deliberately-crashing tool
(`blow_up`, not part of the real contract) used to exercise the MCP protocol-level
`isError=True` path.

`plan_decision`/`log_decision`/`query_data`/`simulate`/`recommend`/`get_contract` are none of them
safety-critical and none of them gated in the real engine -- each just returns its own small,
ungoverned payload here, exactly as a real engine would.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from fastmcp import FastMCP

#: A fake discovery payload for `get_contract`, unrelated to any execute_decision outcome.
NON_ENVELOPE_RESULT = {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

#: `execute_decision` always succeeds for this `decision_id`, echoing back the actual `force`
#: value it received in `payload_summary.force_received` -- lets a test observe, over the real
#: wire, whether `create_algenta_tools`'s argument-level scrub actually reached the server,
#: without needing any in-process shared state or interaction with the idempotency gate below.
FORCE_PROBE_DECISION_ID = "decision-force-probe"

#: `execute_decision` always blocks this `decision_id` with the real `"confidence"` gate unless
#: called with `override_safety=True`.
LOW_CONFIDENCE_DECISION_ID = "decision-low-confidence"

#: `execute_decision` always blocks this `decision_id` with the real `"risk_floor"` gate unless
#: called with `override_safety=True`.
RISK_FLOOR_DECISION_ID = "decision-risk-floor"


def _execution_receipt(
    *,
    decision_id: str,
    webhook_url: str,
    payload_summary: Any = None,
    safety_overridden: bool = False,
    execution_status: str = "delivered",
    response_code: int = 200,
) -> dict[str, Any]:
    """Build a dict shaped exactly like the real `ExecutionReceipt`'s field list."""
    return {
        "decision_id": decision_id,
        "webhook_url": webhook_url,
        "execution_status": execution_status,
        "response_code": response_code,
        "executed_at": "2026-08-23T00:00:00Z",
        "policy_snapshot_id": "policy-snap-1",
        "schema_snapshot_id": "schema-snap-1",
        "manifest_version": "1.0.0",
        "payload_summary": payload_summary if payload_summary is not None else {"decision_id": decision_id},
        "safety_overridden": safety_overridden,
    }


def _execution_denial(*, gate: str, message: str, override_hint: str) -> dict[str, Any]:
    """Build a dict shaped exactly like the real, synchronous 409 body: `{"error": {...}}`."""
    return {"error": {"code": f"execution_blocked_{gate}", "gate": gate, "message": message, "override_hint": override_hint}}


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated delivered-decisions state."""
    mcp = FastMCP("algenta-stub")
    delivered: set[str] = set()

    @mcp.tool
    def get_contract() -> dict:
        """Fake discovery payload -- ungoverned, unrelated to execute_decision."""
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
        """Freeform passthrough, per the real contract -- no gates, no envelope."""
        return {"plan_id": f"plan-{scenario}", "scenario": scenario, "summary": "looks fine"}

    @mcp.tool
    def log_decision(
        chosen_action: str,
        run_id: str | None = None,
        context: dict | None = None,
        options_considered: list | None = None,
        expected_value: float | None = None,
        confidence: float | None = None,
        rationale: str | None = None,
        risk_p5: float | None = None,
        risk_p95: float | None = None,
        risk_pol: float | None = None,
        request_hash: str | None = None,
        result_hash: str | None = None,
    ) -> dict:
        """The real, small `log_decision` result shape -- this is what produces `decision_id`."""
        return {
            "decision_id": f"decision-{chosen_action}",
            "chosen_action": chosen_action,
            "expected_value": expected_value,
            "confidence": confidence,
            "created_at": "2026-08-23T00:00:00Z",
            "note": "logged by stub server",
        }

    @mcp.tool
    def execute_decision(
        decision_id: str,
        webhook_url: str,
        timeout_seconds: float | None = None,
        force: bool = False,
        override_safety: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        """The safety-critical tool: either a real `ExecutionReceipt` (success) or a real,
        synchronous 409-shaped `ExecutionDenial` for exactly one of the three named gates.

        `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
        the real contract's note that `execute_decision` carries these two operator-only fields
        on its real schema -- `create_algenta_tools` is the thing under test for stripping them,
        not this server.
        """
        if decision_id == FORCE_PROBE_DECISION_ID:
            return _execution_receipt(
                decision_id=decision_id,
                webhook_url=webhook_url,
                payload_summary={"force_received": force},
                safety_overridden=force,
            )

        if decision_id == LOW_CONFIDENCE_DECISION_ID and not override_safety:
            return _execution_denial(
                gate="confidence",
                message="decision confidence is below policy.min_confidence.",
                override_hint="Retry with override_safety=true to bypass the confidence gate for this one call.",
            )

        if decision_id == RISK_FLOOR_DECISION_ID and not override_safety:
            return _execution_denial(
                gate="risk_floor",
                message="risk_p5 is below -policy.risk_floor.",
                override_hint="Retry with override_safety=true to bypass the risk-floor gate for this one call.",
            )

        if decision_id in delivered and not force:
            return _execution_denial(
                gate="idempotency",
                message="this decision has already been delivered.",
                override_hint="Retry with force=true to override the idempotency gate for one re-execution.",
            )

        was_already_delivered = decision_id in delivered
        delivered.add(decision_id)
        safety_overridden = (decision_id in (LOW_CONFIDENCE_DECISION_ID, RISK_FLOOR_DECISION_ID) and override_safety) or (
            was_already_delivered and force
        )
        return _execution_receipt(
            decision_id=decision_id,
            webhook_url=webhook_url,
            payload_summary={"decision_id": decision_id, "metadata": metadata},
            safety_overridden=safety_overridden,
        )

    @mcp.tool
    def blow_up() -> dict:
        """Test-only: deliberately raises, to exercise the MCP protocol-level `isError=True`
        path -- a server-side tool crash the MCP SDK turns into ordinary, non-raising response
        data, never a raised exception at any layer above it.
        """
        raise RuntimeError("arbitrary-tool-failure-from-inside-the-stub-server")

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
