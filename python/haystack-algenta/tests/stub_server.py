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

**Corrected against the real engine contract.** An earlier version of this stub modeled
`execute_decision(plan_hash, idempotency_key, force)` returning a generic envelope with an async
`approval_state` (`"none"`/`"pending"`/`"approved"`/`"rejected"`/`"expired"`) that only became
`"approved"` after a separate `_test_approve_plan` administrative call -- mirroring a fictional
plan-hash approval flow that does not exist on the real tool. `execute_decision` for real takes
`decision_id` + `webhook_url` (no `plan_hash`, no caller-supplied idempotency key) and either
succeeds in the same call with a real `ExecutionReceipt`, or is blocked in that same call by
exactly one of three real, named gates (`"idempotency"`, `"confidence"`, `"risk_floor"`) -- see
`haystack_algenta.receipts`. This stub now returns exactly that shape, with two fixed decision-id
fixtures (`CONFIDENCE_BLOCKED_DECISION_ID`, `RISK_FLOOR_BLOCKED_DECISION_ID`) simulating the two
`override_safety`-gated denials, and real per-decision "already delivered" bookkeeping simulating
the `force`-gated idempotency denial for *any* decision id on a second, un-forced call.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

#: `execute_decision` always blocks this decision id with the real `"confidence"` gate unless
#: `override_safety=True` is passed.
CONFIDENCE_BLOCKED_DECISION_ID = "decision-low-confidence"

#: `execute_decision` always blocks this decision id with the real `"risk_floor"` gate unless
#: `override_safety=True` is passed.
RISK_FLOOR_BLOCKED_DECISION_ID = "decision-risky"


def _blocked(*, gate: str, message: str, override_hint: str) -> dict[str, Any]:
    """Build a dict shaped exactly like the real synchronous 409 denial body."""
    return {"error": {"code": f"execution_blocked_{gate}", "gate": gate, "message": message, "override_hint": override_hint}}


def _receipt(
    *,
    decision_id: str,
    webhook_url: str,
    execution_status: str = "delivered",
    safety_overridden: bool = False,
) -> dict[str, Any]:
    """Build a dict shaped exactly like `ExecutionReceipt`'s real field list."""
    return {
        "decision_id": decision_id,
        "webhook_url": webhook_url,
        "execution_status": execution_status,
        "response_code": 200 if execution_status == "delivered" else None,
        "executed_at": "2026-08-23T00:00:00Z",
        "policy_snapshot_id": "policy-1",
        "schema_snapshot_id": "schema-1",
        "manifest_version": "1",
        "payload_summary": {"decision_id": decision_id},
        "safety_overridden": safety_overridden,
    }


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated per-decision delivery state."""
    mcp = FastMCP("algenta-stub")
    delivered_decision_ids: set[str] = set()

    @mcp.tool()
    def get_contract() -> dict:
        """Fake discovery payload -- not an `execute_decision` outcome of any kind."""
        return {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

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
        """Freeform passthrough to `POST /v1/decisions/plan` -- a `DecisionPlan` summary, not
        safety-critical and not gated."""
        return {"plan_id": f"plan-{scenario}", "scenario": scenario, "rationale": "looks fine"}

    @mcp.tool()
    def log_decision(
        chosen_action: str,
        run_id: str | None = None,
        expected_value: float | None = None,
        confidence: float | None = None,
        rationale: str | None = None,
        risk_p5: float | None = None,
        risk_p95: float | None = None,
    ) -> dict:
        """Produces the `decision_id` `execute_decision` needs -- unrelated to any gate."""
        decision_id = f"decision-{chosen_action}"
        return {
            "decision_id": decision_id,
            "chosen_action": chosen_action,
            "expected_value": expected_value,
            "confidence": confidence,
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
        """The safety-critical, gated tool: a same-call 200 `ExecutionReceipt` or a same-call 409
        naming exactly one of the three real gates -- never a pending/async state.

        `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
        the real contract's note that `execute_decision` carries these two operator-only fields on
        its real schema -- the package under test is responsible for stripping them, not this
        server.
        """
        if decision_id == CONFIDENCE_BLOCKED_DECISION_ID and not override_safety:
            return _blocked(
                gate="confidence",
                message="decision confidence 0.32 is below policy.min_confidence 0.60",
                override_hint="Set override_safety=true to bypass the confidence gate.",
            )
        if decision_id == RISK_FLOOR_BLOCKED_DECISION_ID and not override_safety:
            return _blocked(
                gate="risk_floor",
                message="risk_p5 -0.85 is below -policy.risk_floor -0.50",
                override_hint="Set override_safety=true to bypass the risk floor gate.",
            )
        if decision_id in delivered_decision_ids and not force:
            return _blocked(
                gate="idempotency",
                message=f"decision {decision_id!r} was already delivered",
                override_hint="Set force=true to override the idempotency gate for one re-execution.",
            )
        delivered_decision_ids.add(decision_id)
        return _receipt(decision_id=decision_id, webhook_url=webhook_url, safety_overridden=override_safety)

    @mcp.tool()
    def blows_up(decision_id: str) -> dict:
        """Test-only tool: always raises a real Python exception server-side, to prove what a
        genuine MCP-level tool error becomes by the time it reaches a Haystack `Tool.invoke()`
        call and a real `Agent` run (see `test_toolset_scenarios.py`).
        """
        raise RuntimeError(f"engine-side failure for {decision_id}")

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

    def __enter__(self) -> StubServerFixture:
        self._thread.start()
        self._wait_until_serving()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        self._thread.join(timeout=5.0)
