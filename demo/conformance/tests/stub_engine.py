"""A deterministic, in-process HTTP stub for the engine's decision-plan API.

Used by the LangChain conformance-adapter tests to exercise all 9 exercisable scenarios without a
live engine. It is deliberately not a mock of the adapter itself: the adapter still makes real
HTTP requests, and this stub still tracks real lifecycle state (cases, runs, plans, approvals,
executions) so the same assertions the live suite makes against the engine can run unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

#: The fixed pack input every conformance case uses.
PACK_INPUT: dict[str, Any] = {
    "accounts": [
        {
            "account_id": "A1",
            "name": "BigCo",
            "arr": 500000,
            "renewal_date": "2026-08-01",
            "health_score": 40,
            "segment": "standard",
        },
        {
            "account_id": "A5",
            "name": "KeyStrategic",
            "arr": 90000,
            "renewal_date": "2026-08-01",
            "health_score": 50,
            "segment": "strategic",
        },
    ],
    "capacity": {"total_hours": 40},
    "constraints": {"strategic_min_hours": 4, "allocation_step_hours": 1},
    "horizon_days": 90,
}


class _EngineError(Exception):
    """Internal error whose body is returned to the client exactly as-is."""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.body = body


def build_stub_engine_app() -> FastAPI:
    """Build a fresh FastAPI app with isolated decision-plan lifecycle state."""
    app = FastAPI(title="stub-algenta-conformance-engine")

    cases: dict[str, dict[str, Any]] = {}
    runs: dict[str, dict[str, Any]] = {}
    plans: dict[str, dict[str, Any]] = {}
    executed_plans: set[str] = set()

    @app.exception_handler(_EngineError)
    async def _engine_error_handler(request: Request, exc: _EngineError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body)

    def _error(status: int, code: str, message: str) -> _EngineError:
        return _EngineError(status, {"error": {"code": code, "message": message}})

    @app.post("/v1/decision-cases")
    async def create_case(request: Request) -> dict[str, Any]:
        payload = await request.json()
        case_id = f"case-{uuid.uuid4().hex[:12]}"
        cases[case_id] = {
            "case_id": case_id,
            "pack_id": payload.get("pack_id", "renewal_capacity_allocation"),
            "title": payload.get("title", ""),
            "pack_input": payload.get("pack_input", PACK_INPUT),
            "lifecycle_state": "proposed",
        }
        return cases[case_id]

    @app.post("/v1/decision-cases/{case_id}/analysis-runs")
    async def create_analysis_run(case_id: str) -> dict[str, Any]:
        if case_id not in cases:
            raise HTTPException(status_code=404, detail="case not found")
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        runs[run_id] = {"run_id": run_id, "case_id": case_id, "status": "completed"}
        return runs[run_id]

    @app.post("/v1/analysis-runs/{run_id}/action-plans")
    async def create_action_plan(run_id: str) -> dict[str, Any]:
        if run_id not in runs:
            raise HTTPException(status_code=404, detail="run not found")
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        plans[plan_id] = {
            "plan_id": plan_id,
            "case_id": runs[run_id]["case_id"],
            "run_id": run_id,
            "plan_hash": uuid.uuid4().hex,
            "nonce": uuid.uuid4().hex[:16],
            "lifecycle_state": "proposed",
        }
        return plans[plan_id]

    @app.post("/v1/decision-plans/{plan_id}/approve")
    async def approve_plan(plan_id: str, request: Request) -> dict[str, Any]:
        if plan_id not in plans:
            raise HTTPException(status_code=404, detail="plan not found")
        plan = plans[plan_id]
        payload = await request.json()
        submitted_hash = payload.get("plan_hash", "")
        submitted_nonce = payload.get("nonce", "")

        if submitted_hash != plan["plan_hash"]:
            raise _error(403, "plan_hash_mismatch", "submitted plan_hash does not match proposal")
        if plan["lifecycle_state"] == "approved":
            raise _error(409, "plan_not_approvable", "plan has already been approved")
        if submitted_nonce != plan["nonce"]:
            raise _error(403, "invalid_nonce", "submitted nonce is invalid")

        plan["lifecycle_state"] = "approved"
        return {"status": "approved", "plan_id": plan_id}

    @app.post("/v1/decision-plans/{plan_id}/execute")
    async def execute_plan(
        plan_id: str,
        idempotency_key: str | None = Header(default=None),
        traceparent: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if plan_id not in plans:
            raise HTTPException(status_code=404, detail="plan not found")
        plan = plans[plan_id]
        if plan["lifecycle_state"] != "approved":
            raise _error(409, "plan_not_approved", "plan is not approved")
        if plan_id in executed_plans:
            raise _error(409, "plan_not_approved", "plan has already been executed")

        executed_plans.add(plan_id)
        trace_id = None
        if traceparent and traceparent.startswith("00-"):
            trace_id = traceparent.split("-")[1]

        return {
            "lifecycle_state": "executed",
            "plan_id": plan_id,
            "receipt": {
                "receipt_version": "1",
                "execution_id": f"exec-{uuid.uuid4().hex[:12]}",
                "plan_hash": plan["plan_hash"],
                "approval_state": "approved",
                "trace_id": trace_id,
                "idempotency_key": idempotency_key,
                "status": "executed",
            },
        }

    @app.get("/v1/decision-cases/{case_id}")
    async def get_case(case_id: str) -> dict[str, Any]:
        if case_id not in cases:
            raise HTTPException(status_code=404, detail="case not found")
        return cases[case_id]

    @app.get("/v1/audit-logs/export.json")
    async def export_audit_logs() -> Response:
        raw = b"[]"
        sha = hashlib.sha256(raw).hexdigest()
        return Response(
            content=raw,
            media_type="application/json",
            headers={"X-Content-SHA256": sha, "X-Audit-Export-Rows": "0"},
        )

    return app


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


class StubEngineFixture:
    """Async context manager that runs the stub engine on an ephemeral port."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self.base_url: str = ""

    async def __aenter__(self) -> StubEngineFixture:
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, port = sock.getsockname()
        self._sock = sock

        app = build_stub_engine_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
        await _wait_until_serving(port)
        self.base_url = f"http://127.0.0.1:{port}"
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
