"""A deterministic, self-contained demo Algenta Engine for recipe runs and tests.

This is a real `mcp.server.fastmcp.FastMCP` server (the base MCP SDK's own FastMCP -- a runtime
dependency of this package), servable over a real HTTP socket on `127.0.0.1` -- not an
in-memory transport shortcut and not a mock of `temporal_algenta.client.AlgentaMcpClient`'s
internals. Both the recipes' `main()` runners (via `recipes/_runner.py`) and the test suite
(via `tests/stub_server.py`) exercise the real `AlgentaActivities` -> real `AlgentaMcpClient`
-> real `mcp` client -> real wire -> this server round trip, so a wire-shape regression (e.g. a
denial not surviving the real MCP error-content round trip) is actually caught, unlike a test
that mocks tool execution directly.

Nothing here talks to any real Algenta Engine -- none is needed to run a recipe or the tests.
`execute_decision` below is shaped like the real, documented governed-execution contract (see
`temporal_algenta.receipts`): it either returns a real `ExecutionReceipt`, or raises -- exactly
like a real MCP tool wrapping a caught HTTP `409` would -- causing FastMCP to report
`CallToolResult(isError=True)` with the real `{"error": {...}}` denial body as its text
content. Every other tool is a plain, freeform result -- `plan_decision` / `log_decision` /
`query_data` / `simulate` / `recommend` / `get_contract` are not safety-critical and carry no
gates on the real engine, so they don't return anything shaped like `ExecutionReceipt` or a
denial either.

Everything is deterministic: fixed timestamps, fixed policy/schema snapshot ids, and derived
(not random) simulation values, so recipe output is reproducible run after run.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

#: A `decision_id` `execute_decision` always reports as blocked on the `"confidence"` gate,
#: regardless of prior calls -- simulates a logged decision whose `confidence` never clears
#: `policy.min_confidence`. Reachable by logging a decision with `chosen_action="low-confidence"`.
LOW_CONFIDENCE_DECISION_ID = "decision-low-confidence"
CONFIDENCE_GATE_CODE = "execution_blocked_confidence"

#: A `decision_id` `execute_decision` always reports as blocked on the `"risk_floor"` gate.
#: Reachable by logging a decision with `chosen_action="risky"`.
BELOW_RISK_FLOOR_DECISION_ID = "decision-risky"
RISK_FLOOR_GATE_CODE = "execution_blocked_risk_floor"

#: A `decision_id` whose webhook delivery always comes back as a real, successful receipt with
#: `execution_status="failed"` -- the call itself completed; the target endpoint just didn't
#: accept the delivery. Not a policy denial at all. Reachable via `chosen_action="webhook-down"`.
FAILED_DELIVERY_DECISION_ID = "decision-webhook-down"

IDEMPOTENCY_GATE_CODE = "execution_blocked_idempotency"

#: A `query_data` dataset that stands in for the engine's own audit-trail dataset: rows are
#: derived deterministically from this engine's logged decisions and delivered executions.
AUDIT_DATASET = "audit_log"

#: A `query_data` dataset whose first two queries (per engine instance) report a generic,
#: non-denial tool-execution error -- an engine-side transient ("engine restarted mid-call"),
#: exactly the shape that must stay *retryable* (see `recipes/retry_policy_structured_denials.py`).
FLAKY_DATASET = "flaky"
FLAKY_DATASET_FAILURES = 2

#: The fixed clock every demo-engine timestamp uses -- determinism for reproducible recipes.
DEMO_CLOCK = "2026-09-01T00:00:00Z"


class DemoAlgentaEngine:
    """One isolated demo engine: its own FastMCP server plus the state its tools close over.

    State is plain, inspectable data (`delivered_decision_ids`, `logged_decisions`,
    `query_attempts`) so tests and recipe output can assert on what the "engine" actually did
    -- e.g. that a non-retryable denial caused exactly one `execute_decision` call.
    """

    def __init__(self) -> None:
        self.delivered_decision_ids: set[str] = set()
        self.logged_decisions: list[dict[str, Any]] = []
        self.query_attempts: dict[str, int] = {}
        self.execute_attempts: dict[str, int] = {}
        self.server = self._build_server()

    def reset(self) -> None:
        """Clear all mutable state, giving the next test a pristine engine against the same
        running server. The tools read these attributes at call time (closures over `self`),
        so clearing them here is exactly equivalent to a fresh engine.
        """
        self.delivered_decision_ids.clear()
        self.logged_decisions.clear()
        self.query_attempts.clear()
        self.execute_attempts.clear()

    def _receipt(
        self,
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
            "executed_at": DEMO_CLOCK,
            "policy_snapshot_id": "policy-snap-1",
            "schema_snapshot_id": "schema-snap-1",
            "manifest_version": "1.0.0",
            "payload_summary": {"decision_id": decision_id},
            "safety_overridden": safety_overridden,
        }

    @staticmethod
    def _blocked(code: str, gate: str, message: str, override_hint: str) -> Exception:
        """Build the exception `execute_decision` raises for a blocked gate.

        Raising (rather than returning a dict) is what makes FastMCP report this as
        `CallToolResult(isError=True)` -- exactly the real, synchronous `409` behavior this demo
        tool is standing in for -- with `str(exception)` (the JSON body below) as the error's
        text content, mirroring how a real MCP tool wrapping a caught HTTP `409` would
        stringify its body.
        """
        return RuntimeError(
            json.dumps({"error": {"code": code, "gate": gate, "message": message, "override_hint": override_hint}})
        )

    def _build_server(self) -> FastMCP:
        # Stateless streamable-HTTP mode: every request is self-contained, so there is no
        # server-side MCP session manager at all. The engine's own state lives in this
        # `DemoAlgentaEngine` instance (plain attributes the tools close over), not in MCP
        # sessions -- nothing about the tool contract needs server-side session state, and
        # the session manager's initialize race (an intermittent upstream-SDK stall where the
        # initialize response body never arrives, reproduced directly) is removed from the
        # recipe/test wire entirely. Clients still speak the same streamable-HTTP MCP
        # protocol to it.
        mcp = FastMCP("algenta-demo-engine", stateless_http=True)

        @mcp.tool()
        def get_contract() -> dict:
            """Demo discovery payload -- deliberately not an execution receipt."""
            return {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

        @mcp.tool()
        def query_data(dataset: str) -> dict:
            self.query_attempts[dataset] = self.query_attempts.get(dataset, 0) + 1
            if dataset == FLAKY_DATASET and self.query_attempts[dataset] <= FLAKY_DATASET_FAILURES:
                raise RuntimeError(f"engine restarted mid-query; dataset {dataset!r} temporarily unavailable")
            if dataset == AUDIT_DATASET:
                rows: list[dict[str, Any]] = [
                    {"event": "decision_logged", "decision_id": d["decision_id"], "at": DEMO_CLOCK}
                    for d in self.logged_decisions
                ]
                rows += [
                    {"event": "decision_delivered", "decision_id": decision_id, "at": DEMO_CLOCK}
                    for decision_id in sorted(self.delivered_decision_ids)
                ]
                return {"dataset": dataset, "rows": rows}
            return {"dataset": dataset, "rows": [{"value": 1}, {"value": 2}]}

        @mcp.tool()
        def simulate(scenario: str) -> dict:
            """Deterministic demo simulation: the expected value is derived from the scenario
            string (never random), so identical input always yields identical output."""
            expected_value = float((len(scenario) * 13) % 97)
            return {
                "scenario": scenario,
                "expected_value": expected_value,
                "confidence": 0.87,
                "risk_p5": -expected_value / 10.0,
            }

        @mcp.tool()
        def recommend(scenario: str) -> dict:
            return {"scenario": scenario, "recommended_action": "hold", "confidence": 0.87}

        @mcp.tool()
        def plan_decision(scenario: str) -> dict:
            """Freeform passthrough -- not safety-critical, no gates. Returns a
            `DecisionPlan`-shaped summary, not an execution receipt."""
            return {"plan_id": f"plan-{scenario}", "scenario": scenario, "rationale": "looks fine"}

        @mcp.tool()
        def log_decision(chosen_action: str, rationale: str | None = None, note: str | None = None) -> dict:
            decision_id = f"decision-{chosen_action}"
            record = {
                "decision_id": decision_id,
                "chosen_action": chosen_action,
                "rationale": rationale,
                "expected_value": 42.0,
                "confidence": 0.91,
                "created_at": DEMO_CLOCK,
                "note": note,
            }
            self.logged_decisions.append(record)
            return record

        @mcp.tool()
        def execute_decision(
            decision_id: str,
            webhook_url: str,
            timeout_seconds: float = 30.0,
            force: bool = False,
            override_safety: bool = False,
        ) -> dict:
            """The real, safety-critical, synchronously-gated tool.

            Exactly three named gates, matching the real engine:
            - `"idempotency"`: this `decision_id` was already delivered and `force` wasn't
              `true`. `force` bypasses *only* this gate, for one re-execution.
            - `"confidence"`: `LOW_CONFIDENCE_DECISION_ID`, unless `override_safety=true`.
            - `"risk_floor"`: `BELOW_RISK_FLOOR_DECISION_ID`, unless `override_safety=true`.
            A call that clears all applicable gates returns a real `ExecutionReceipt`,
            synchronously, in this same call -- there is no "pending" outcome at all.
            """
            self.execute_attempts[decision_id] = self.execute_attempts.get(decision_id, 0) + 1
            if decision_id in self.delivered_decision_ids and not force:
                raise self._blocked(
                    IDEMPOTENCY_GATE_CODE,
                    "idempotency",
                    f"Decision {decision_id!r} was already delivered.",
                    "Pass force=true to override the idempotency gate for one re-execution.",
                )
            if decision_id == LOW_CONFIDENCE_DECISION_ID and not override_safety:
                raise self._blocked(
                    CONFIDENCE_GATE_CODE,
                    "confidence",
                    "Decision confidence is below policy.min_confidence.",
                    "Pass override_safety=true to bypass the confidence gate.",
                )
            if decision_id == BELOW_RISK_FLOOR_DECISION_ID and not override_safety:
                raise self._blocked(
                    RISK_FLOOR_GATE_CODE,
                    "risk_floor",
                    "risk_p5 is below -policy.risk_floor.",
                    "Pass override_safety=true to bypass the risk floor gate.",
                )

            self.delivered_decision_ids.add(decision_id)
            if decision_id == FAILED_DELIVERY_DECISION_ID:
                return self._receipt(decision_id, webhook_url, execution_status="failed", response_code=503)
            return self._receipt(decision_id, webhook_url, safety_overridden=override_safety)

        return mcp


async def _wait_until_serving(server: object, *, timeout: float = 10.0) -> None:
    """Wait until uvicorn has fully started -- lifespan included, not just the socket.

    A bare TCP-connect probe is NOT a readiness signal here: the socket is bound and listening
    (backlog) before `uvicorn.Server.serve` has run the ASGI lifespan, and FastMCP's streamable
    HTTP session manager is only ready to route requests once that lifespan has run. A client
    whose initialize POST lands in that window can hang indefinitely (flaky, and only visible on
    the second sequential server, when imports are warm and startup outraces the client). The
    `Server.started` flag is set only after startup -- lifespan included -- completes.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while not getattr(server, "started", False):
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("uvicorn server did not report started within the timeout")
        await asyncio.sleep(0.02)


@asynccontextmanager
async def serve_demo_engine(engine: DemoAlgentaEngine | None = None) -> AsyncIterator[tuple[str, DemoAlgentaEngine]]:
    """Serve one `DemoAlgentaEngine` over a real HTTP socket on an ephemeral `127.0.0.1` port.

    Uses a manually bound socket + `uvicorn.Server(...).serve(sockets=[sock])` rather than
    `FastMCP.run_streamable_http_async()` directly, since that method doesn't accept a
    pre-bound socket or ephemeral-port discovery -- binding our own socket on port 0 first is
    what lets every caller get its own isolated engine on its own free port.

    Yields `(base_url, engine)`; `base_url` is ready to pass to
    `AlgentaActivities(base_url=...)`.
    """
    import uvicorn

    if engine is None:
        engine = DemoAlgentaEngine()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    _, port = sock.getsockname()

    app = engine.server.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await _wait_until_serving(server)
        yield f"http://127.0.0.1:{port}/mcp", engine
    finally:
        # Graceful shutdown via uvicorn's own `should_exit` flag -- NOT a bare task.cancel().
        # Cancelling serve() mid-flight strands the FastMCP app's lifespan half-shut (its
        # session-manager task group never finishes unwinding), and the NEXT uvicorn server
        # started on this event loop then accepts connections but never answers requests --
        # reproduced directly (second sequential fixture's first request times out). Every
        # step is bounded: a wedged shutdown must never hang the suite (worst case the serve
        # task is abandoned; the test's event loop closes right after and discards it).
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                pass
        sock.close()


__all__ = [
    "AUDIT_DATASET",
    "BELOW_RISK_FLOOR_DECISION_ID",
    "CONFIDENCE_GATE_CODE",
    "DEMO_CLOCK",
    "FAILED_DELIVERY_DECISION_ID",
    "FLAKY_DATASET",
    "FLAKY_DATASET_FAILURES",
    "IDEMPOTENCY_GATE_CODE",
    "LOW_CONFIDENCE_DECISION_ID",
    "RISK_FLOOR_GATE_CODE",
    "DemoAlgentaEngine",
    "serve_demo_engine",
]
