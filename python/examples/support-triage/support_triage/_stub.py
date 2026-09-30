"""A deterministic, fake self-hosted Algenta MCP server for the support-triage demo/tests.

This is the same real `mcp.server.fastmcp.FastMCP` server pattern the integration packages
use in their own suites: a real MCP server over a real HTTP socket on `127.0.0.1`, standing
in for a caller's own self-hosted engine. No proprietary service is contacted.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP

#: A `decision_id` `execute_decision` always reports as blocked on the `"confidence"` gate.
LOW_CONFIDENCE_DECISION_ID = "decision-low-confidence"
CONFIDENCE_GATE_CODE = "execution_blocked_confidence"

#: A `decision_id` `execute_decision` always reports as blocked on the `"risk_floor"` gate.
BELOW_RISK_FLOOR_DECISION_ID = "decision-risky"
RISK_FLOOR_GATE_CODE = "execution_blocked_risk_floor"

#: A `decision_id` whose webhook delivery always comes back as a receipt with
#: `execution_status="failed"` -- the call completed; the target endpoint rejected delivery.
FAILED_DELIVERY_DECISION_ID = "decision-webhook-down"

IDEMPOTENCY_GATE_CODE = "execution_blocked_idempotency"

NON_ENVELOPE_RESULT = {
    "capabilities": ["query", "simulate", "recommend"],
    "engine_version": "1.4.0",
}


def _receipt(
    decision_id: str,
    webhook_url: str,
    *,
    execution_status: str = "delivered",
    response_code: int = 200,
    safety_overridden: bool = False,
) -> dict[str, Any]:
    """Build a dict shaped exactly like the real `execute_decision` success envelope."""
    return {
        "decision_id": decision_id,
        "webhook_url": webhook_url,
        "execution_status": execution_status,
        "response_code": response_code,
        "executed_at": "2026-08-23T00:00:00Z",
        "policy_snapshot_id": "policy-snap-1",
        "schema_snapshot_id": "schema-snap-1",
        "manifest_version": "1.0.0",
        "payload_summary": {"decision_id": decision_id},
        "safety_overridden": safety_overridden,
    }


def _blocked(code: str, gate: str, message: str, override_hint: str) -> Exception:
    """Build the exception `execute_decision` raises for a blocked gate."""
    return RuntimeError(
        json.dumps(
            {
                "error": {
                    "code": code,
                    "gate": gate,
                    "message": message,
                    "override_hint": override_hint,
                }
            }
        )
    )


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated delivery state."""
    mcp = FastMCP("algenta-stub")
    delivered_decision_ids: set[str] = set()

    @mcp.tool()
    def get_contract() -> dict:
        """Fake discovery payload -- deliberately not an execution receipt."""
        return dict(NON_ENVELOPE_RESULT)

    @mcp.tool()
    def query_data(dataset: str) -> dict:
        return {"dataset": dataset, "rows": [{"value": 1}, {"value": 2}]}

    @mcp.tool()
    def simulate(scenario: str) -> dict:
        return {"scenario": scenario, "expected_value": 42.0}

    @mcp.tool()
    def recommend(scenario: str) -> dict:
        return {"scenario": scenario, "recommended_action": "hold", "confidence": 0.87}

    @mcp.tool()
    def plan_decision(scenario: str) -> dict:
        """Freeform passthrough -- not safety-critical, no gates."""
        return {"plan_id": f"plan-{scenario}", "scenario": scenario, "rationale": "looks fine"}

    @mcp.tool()
    def log_decision(chosen_action: str) -> dict:
        decision_id = f"decision-{chosen_action}"
        return {
            "decision_id": decision_id,
            "chosen_action": chosen_action,
            "expected_value": 42.0,
            "confidence": 0.91,
            "created_at": "2026-08-23T00:00:00Z",
            "note": None,
        }

    @mcp.tool()
    def execute_decision(
        decision_id: str,
        webhook_url: str,
        timeout_seconds: float = 30.0,
        force: bool = False,
        override_safety: bool = False,
    ) -> dict:
        """The real, safety-critical, synchronously-gated tool.

        Mirrors the three named policy gates a real self-hosted Algenta engine enforces:
        `idempotency`, `confidence`, and `risk_floor`. There is no "pending" outcome.
        """
        if decision_id in delivered_decision_ids and not force:
            raise _blocked(
                IDEMPOTENCY_GATE_CODE,
                "idempotency",
                f"Decision {decision_id!r} was already delivered.",
                "Pass force=true to override the idempotency gate for one re-execution.",
            )
        if decision_id == LOW_CONFIDENCE_DECISION_ID and not override_safety:
            raise _blocked(
                CONFIDENCE_GATE_CODE,
                "confidence",
                "Decision confidence is below policy.min_confidence.",
                "Pass override_safety=true to bypass the confidence gate.",
            )
        if decision_id == BELOW_RISK_FLOOR_DECISION_ID and not override_safety:
            raise _blocked(
                RISK_FLOOR_GATE_CODE,
                "risk_floor",
                "risk_p5 is below -policy.risk_floor.",
                "Pass override_safety=true to bypass the risk floor gate.",
            )

        delivered_decision_ids.add(decision_id)
        if decision_id == FAILED_DELIVERY_DECISION_ID:
            return _receipt(decision_id, webhook_url, execution_status="failed", response_code=503)
        return _receipt(decision_id, webhook_url, safety_overridden=override_safety)

    @mcp.tool()
    def _test_diagnostics() -> dict:
        """Test-only diagnostic surface reachable via the `full` profile."""
        return {"delivered_count": len(delivered_decision_ids)}

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
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, port = sock.getsockname()
        self._sock = sock

        mcp = build_stub_algenta_server()
        app = mcp.streamable_http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
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
