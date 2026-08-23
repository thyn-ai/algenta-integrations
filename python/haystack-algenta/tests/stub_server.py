"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests, run on a real HTTP
socket on its own background thread.

This is a real `mcp.server.fastmcp.FastMCP` server (the base MCP SDK's own FastMCP -- already a
transitive dependency of `mcp-haystack`'s MCP client support, so no extra `fastmcp` package is
needed) -- not an in-memory transport shortcut and not a mock of `haystack_algenta`'s internals.
Tests exercise the real `haystack_algenta.create_algenta_tools` -> real
`haystack_integrations.tools.mcp.MCPToolset` -> real `mcp` client -> real wire -> this server round
trip, so a wire-shape regression (e.g. the receipt envelope not surviving the MCP text-content
round trip) would actually be caught here, unlike a test that mocks tool execution directly.

Runs on its own background thread with its own event loop, not on whatever thread/loop a test's
(synchronous) `MCPToolset`/`Agent` calls happen to run on: `MCPToolset`'s public API is
synchronous and blocks the calling thread on its own internal `AsyncExecutor` -- if the stub
server were served on that same thread, it could never accept the connection, exactly like a real,
separately-running Algenta Engine process would need its own thread/process regardless.

Identical in shape (same plan-hash fixtures, same scenarios, same tool set) to its siblings in
`python/pydantic-ai-algenta/tests/stub_server.py`, `python/langchain-algenta/tests/stub_server.py`,
and `python/maf-algenta/tests/stub_server.py` -- deliberately not reinvented per package, so a
behavioral difference across the Python integrations would show up as a real test divergence, not
get lost in differently-shaped fixtures. Only the threading wrapper (`StubServerFixture` below)
differs, because Haystack's real primitive here is synchronous where the other four packages' are
async.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

#: A plan whose first `execute_decision` call always comes back pending, and which becomes
#: `approval_state="approved"` only after `_test_approve_plan` has been called for it.
PENDING_PLAN_HASH = "plan-needs-approval"

#: A plan `execute_decision` always rejects outright with a named policy-gate code, regardless of
#: approval state -- simulates a stale/mismatched plan the engine refuses to run at all.
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

    @mcp.tool()
    def get_contract() -> dict:
        """Fake discovery payload -- deliberately not a governed-execution envelope."""
        return dict(NON_ENVELOPE_RESULT)

    @mcp.tool()
    def query_data(dataset: str) -> dict:
        return _receipt(result={"dataset": dataset, "rows": [{"value": 1}, {"value": 2}]})

    @mcp.tool()
    def simulate(scenario: str) -> dict:
        return _receipt(result={"scenario": scenario, "expected_value": 42.0})

    @mcp.tool()
    def recommend(scenario: str) -> dict:
        return _receipt(result={"scenario": scenario, "recommended_action": "hold", "confidence": 0.87})

    @mcp.tool()
    def plan_decision(scenario: str) -> dict:
        plan_hash = f"plan-{scenario}"
        return _receipt(plan_hash=plan_hash, result={"plan_hash": plan_hash, "rationale": "looks fine"})

    @mcp.tool()
    def log_decision(plan_hash: str) -> dict:
        return _receipt(plan_hash=plan_hash, result={"logged": True})

    @mcp.tool()
    def execute_decision(plan_hash: str, idempotency_key: str = "idem-1", force: bool = False) -> dict:
        """The safety-critical, approval-gated tool.

        `force` is declared on this fake tool's schema on purpose, mirroring the real contract's
        note that `execute_decision` carries an operator-only `force` field on its real schema --
        the package under test is responsible for stripping it, not this server.
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
                result={"executed": True, "plan_hash": plan_hash, "forced": force},
            )
        return _receipt(
            approval_state="pending",
            plan_hash=plan_hash,
            execution_id=execution_id,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    def blows_up(plan_hash: str) -> dict:
        """Test-only tool: always raises a real Python exception server-side, to prove what a
        genuine MCP-level tool error becomes by the time it reaches a Haystack `Tool.invoke()`
        call and a real `Agent` run (see `test_toolset_scenarios.py`).
        """
        raise RuntimeError(f"engine-side failure for {plan_hash}")

    @mcp.tool()
    def _test_approve_plan(plan_hash: str) -> dict:
        """Test-only: stand-in for a human approving the plan via the engine's real HTTP
        endpoint. Not part of the real Algenta MCP tool registry or the tool-profile contract.
        """
        approved_plans.add(plan_hash)
        return {"approved": True, "plan_hash": plan_hash}

    return mcp


class StubServerFixture:
    """Runs `build_stub_algenta_server()` over a real HTTP socket on its own background thread.

    `MCPToolset`'s own public API is synchronous (it blocks the calling thread on an internal
    `AsyncExecutor`), so unlike the async `StubServerFixture` this package's siblings use, this
    one is a plain synchronous context manager wrapping a background thread with its own asyncio
    event loop -- otherwise the server and the client under test would contend for the same
    thread and the connection could never complete.
    """

    def __init__(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, port = sock.getsockname()
        self._sock = sock
        self.base_url = f"http://127.0.0.1:{port}/mcp"
        self._server: Any = None
        self._thread = threading.Thread(target=self._run, args=(port,), daemon=True)

    def _run(self, port: int) -> None:
        import asyncio

        import uvicorn

        mcp = build_stub_algenta_server()
        app = mcp.streamable_http_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        asyncio.run(self._server.serve(sockets=[self._sock]))

    def _wait_until_serving(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        host_port = self.base_url.split("://", 1)[1].split("/", 1)[0]
        host, port_str = host_port.split(":")
        port = int(port_str)
        while time.time() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        raise TimeoutError("stub server did not start in time")

    def __enter__(self) -> "StubServerFixture":
        self._thread.start()
        self._wait_until_serving()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self._thread.join(timeout=5.0)
