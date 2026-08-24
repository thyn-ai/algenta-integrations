"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `mcp.server.fastmcp.FastMCP` server (the base MCP SDK's own FastMCP -- already a
transitive dependency of `langchain-mcp-adapters`, so no extra `fastmcp` package is needed), run
over a real HTTP socket on `127.0.0.1` -- not an in-memory transport shortcut and not a mock of
`AlgentaToolCallInterceptor`'s internals. Tests exercise the real
`langchain_algenta.create_algenta_tools` -> real `langchain_mcp_adapters.MultiServerMCPClient` ->
real `mcp` client -> real wire -> this server round trip, so a wire-shape regression (e.g. a
denial not surviving the real MCP error-content round trip) would actually be caught here, unlike
a test that mocks tool execution directly.

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
`execute_decision` below is the only tool shaped like the real, documented governed-execution
contract (see `langchain_algenta.receipts`): it either returns a real `ExecutionReceipt`, or
raises -- exactly like a real MCP tool wrapping a caught HTTP `409` would -- causing FastMCP to
report `CallToolResult(isError=True)` with the real `{"error": {...}}` denial body as its text
content (verified directly against the installed `mcp` SDK's
`Server._make_error_result(str(exception))`). Every other tool below is a plain, freeform
result -- `plan_decision` / `log_decision` / `query_data` / `simulate` / `recommend` /
`get_contract` are not safety-critical and carry no gates at all on the real engine, so they
don't return anything shaped like `ExecutionReceipt` or a denial either.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP

#: A `decision_id` `execute_decision` always reports as blocked on the `"confidence"` gate,
#: regardless of prior calls -- simulates a logged decision whose `confidence` never clears
#: `policy.min_confidence`.
LOW_CONFIDENCE_DECISION_ID = "decision-low-confidence"
CONFIDENCE_GATE_CODE = "execution_blocked_confidence"

#: A `decision_id` `execute_decision` always reports as blocked on the `"risk_floor"` gate.
BELOW_RISK_FLOOR_DECISION_ID = "decision-risky"
RISK_FLOOR_GATE_CODE = "execution_blocked_risk_floor"

#: A `decision_id` whose webhook delivery always comes back as a real, successful receipt with
#: `execution_status="failed"` -- the call itself completed; the target endpoint just didn't
#: accept the delivery. Not a policy denial at all.
FAILED_DELIVERY_DECISION_ID = "decision-webhook-down"

IDEMPOTENCY_GATE_CODE = "execution_blocked_idempotency"

#: A tool whose result is intentionally *not* an `ExecutionReceipt`, to exercise the passthrough
#: path for tools that don't return one (e.g. a real `get_contract` discovery blob).
NON_ENVELOPE_RESULT = {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}


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
    """Build the exception `execute_decision` raises for a blocked gate.

    Raising (rather than returning a dict) is what makes FastMCP report this as
    `CallToolResult(isError=True)` -- exactly the real, synchronous `409` behavior this fake
    tool is standing in for -- with `str(exception)` (the JSON body below) as the error's text
    content, mirroring how a real MCP tool wrapping a caught HTTP `409` would stringify its body.
    """
    return RuntimeError(
        json.dumps({"error": {"code": code, "gate": gate, "message": message, "override_hint": override_hint}})
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
        """Freeform passthrough -- not safety-critical, no gates. Returns a `DecisionPlan`-shaped
        summary, not an execution receipt."""
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

        `force` / `override_safety` are declared on this fake tool's schema on purpose,
        mirroring the real contract's note that `execute_decision` carries these two
        operator-only fields on its real schema -- the package under test is responsible for
        stripping them, not this server.

        Exactly three named gates, matching the real engine:
        - `"idempotency"`: this `decision_id` was already delivered and `force` wasn't `true`.
          `force` bypasses *only* this gate, for one re-execution.
        - `"confidence"`: `LOW_CONFIDENCE_DECISION_ID`, unless `override_safety=true`.
        - `"risk_floor"`: `BELOW_RISK_FLOOR_DECISION_ID`, unless `override_safety=true`.
        A call that clears all applicable gates returns a real `ExecutionReceipt`, synchronously,
        in this same call -- there is no "pending" outcome at all.
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
        """Test-only: stands in for the wider admin/ops tooling a real engine's MCP registry
        advertises beyond the four contract profiles' named tools (e.g. queue depth, connector
        health). Not part of the real tool-profile contract at all -- exists here only to prove
        that `profile="full"` genuinely exposes tools the other three profiles never list."""
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
    """Async context manager that runs `build_stub_algenta_server()` over a real HTTP socket.

    Uses a manually bound socket + `uvicorn.Server(...).serve(sockets=[sock])` rather than
    `FastMCP.run_streamable_http_async()` directly, since that method doesn't accept a
    pre-bound socket or ephemeral-port discovery -- binding our own socket on port 0 first is
    what lets every test get its own isolated server on its own free port.
    """

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
