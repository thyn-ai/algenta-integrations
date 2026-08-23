"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `fastmcp.FastMCP` server, run over a real HTTP socket on `127.0.0.1` -- not an
in-memory transport shortcut and not a mock of `AlgentaToolset`'s internals. Tests exercise the
real `AlgentaToolset` -> real `pydantic_ai.mcp.MCPToolset` -> real `fastmcp.Client` -> real wire
-> this server round trip, so a wire-shape regression (e.g. the receipt envelope not surviving
structured-content round-tripping) would actually be caught here, unlike a test that mocks
`call_tool` directly.

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
Every tool below is a hand-built fake shaped like the real, documented governed-execution
envelope (see `pydantic_ai_algenta.receipts.GovernedExecutionReceipt`), plus one test-only
administrative tool (`_test_approve_plan`, not part of the real contract) that lets a test
simulate "a human approved this plan through the engine's real HTTP endpoint" without an actual
engine to call.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from fastmcp import FastMCP

#: A plan whose first `execute_decision` call always comes back pending, and which becomes
#: `approval_state="approved"` only after `_test_approve_plan` has been called for it.
PENDING_PLAN_HASH = "plan-needs-approval"

#: A plan `execute_decision` always rejects outright with a named policy-gate code, regardless
#: of approval state -- simulates a stale/mismatched plan the engine refuses to run at all.
REJECTED_PLAN_HASH = "plan-stale-hash"
REJECTED_PLAN_CODE = "stale_plan"

#: A tool whose result is intentionally *not* a governed-execution envelope, to exercise the
#: passthrough path for tools that don't return one (e.g. a real `get_contract` discovery blob).
NON_ENVELOPE_RESULT = {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}


def _receipt(
    *,
    status: str = "ok",
    code: str = "ok",
    retryable: bool = False,
    approval_state: str = "none",
    plan_hash: str | None = None,
    execution_id: str | None = None,
    idempotency_key: str | None = None,
    result: Any = None,
) -> dict[str, Any]:
    """Build a dict shaped exactly like `GovernedExecutionReceipt`'s real field list."""
    return {
        "status": status,
        "code": code,
        "retryable": retryable,
        "request_id": f"req-{code}",
        "trace_id": f"trace-{code}",
        "policy_snapshot_hash": "snap-1",
        "receipt_version": 1,
        "plan_hash": plan_hash,
        "approval_state": approval_state,
        "execution_id": execution_id,
        "idempotency_key": idempotency_key,
        "result": result,
    }


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated approval state."""
    mcp = FastMCP("algenta-stub")
    approved_plans: set[str] = set()

    @mcp.tool
    def get_contract() -> dict:
        """Fake discovery payload -- deliberately not a governed-execution envelope."""
        return dict(NON_ENVELOPE_RESULT)

    @mcp.tool
    def query_data(dataset: str) -> dict:
        return _receipt(result={"dataset": dataset, "rows": [{"value": 1}, {"value": 2}]})

    @mcp.tool
    def simulate(scenario: str) -> dict:
        return _receipt(result={"scenario": scenario, "expected_value": 42.0})

    @mcp.tool
    def recommend(scenario: str) -> dict:
        return _receipt(result={"scenario": scenario, "recommended_action": "hold", "confidence": 0.87})

    @mcp.tool
    def plan_decision(scenario: str) -> dict:
        plan_hash = f"plan-{scenario}"
        return _receipt(plan_hash=plan_hash, result={"plan_hash": plan_hash, "rationale": "looks fine"})

    @mcp.tool
    def log_decision(plan_hash: str) -> dict:
        return _receipt(plan_hash=plan_hash, result={"logged": True})

    @mcp.tool
    def execute_decision(plan_hash: str, idempotency_key: str = "idem-1", force: bool = False) -> dict:
        """The safety-critical, approval-gated tool.

        `force` is declared on this fake tool's schema on purpose, mirroring the real
        contract's note that `execute_decision` carries an operator-only `force` field on its
        real schema -- `AlgentaToolset` is the thing under test for stripping it, not this
        server.
        """
        execution_id = f"exec-{plan_hash}"
        if plan_hash == REJECTED_PLAN_HASH:
            return _receipt(
                status="error",
                code=REJECTED_PLAN_CODE,
                approval_state="rejected",
                plan_hash=plan_hash,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
            )
        if plan_hash in approved_plans:
            return _receipt(
                approval_state="approved",
                plan_hash=plan_hash,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                result={"executed": True, "plan_hash": plan_hash},
            )
        return _receipt(
            approval_state="pending",
            plan_hash=plan_hash,
            execution_id=execution_id,
            idempotency_key=idempotency_key,
        )

    @mcp.tool
    def _test_approve_plan(plan_hash: str) -> dict:
        """Test-only: stand-in for a human approving the plan via the engine's real HTTP
        endpoint. Not part of the real Algenta MCP tool registry or the tool-profile contract.
        """
        approved_plans.add(plan_hash)
        return {"approved": True, "plan_hash": plan_hash}

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
