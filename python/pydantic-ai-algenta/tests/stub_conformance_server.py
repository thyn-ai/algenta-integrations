"""A deterministic, fake self-hosted Algenta engine for the demo conformance suite.

This is a real `starlette` ASGI application run over a real HTTP socket on `127.0.0.1` -- not a
mock of `pydantic_ai_algenta.conformance`'s internals. It mirrors the engine's decision-case /
analysis-run / decision-plan / audit REST surface that the demo conformance scenarios exercise,
so a wire-shape regression (e.g. a named error code changing) would actually be caught here.

Nothing here talks to any real Algenta engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import uuid
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


def _json_response(data: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code)


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return _json_response({"error": {"code": code, "message": message}}, status_code=status_code)


class StubConformanceEngine:
    """In-memory state for the fake engine."""

    def __init__(self) -> None:
        self.cases: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}

    def create_case(self, body: dict[str, Any]) -> dict[str, Any]:
        case_id = f"case-{uuid.uuid4().hex[:12]}"
        case = {
            "case_id": case_id,
            "pack_id": body.get("pack_id", "renewal_capacity_allocation"),
            "title": body.get("title", "conformance case"),
            "pack_input": body.get("pack_input", {}),
        }
        self.cases[case_id] = case
        return case

    def create_run(self, case_id: str) -> dict[str, Any]:
        if case_id not in self.cases:
            raise KeyError(case_id)
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        run = {"run_id": run_id, "case_id": case_id, "status": "completed"}
        self.runs[run_id] = run
        return run

    def create_plan(self, run_id: str) -> dict[str, Any]:
        if run_id not in self.runs:
            raise KeyError(run_id)
        run = self.runs[run_id]
        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        plan_hash = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars
        nonce = uuid.uuid4().hex[:16]
        plan = {
            "plan_id": plan_id,
            "run_id": run_id,
            "case_id": run["case_id"],
            "plan_hash": plan_hash,
            "nonce": nonce,
            "lifecycle_state": "proposed",
        }
        self.plans[plan_id] = plan
        return plan

    def approve_plan(
        self, plan_id: str, plan_hash: str, nonce: str
    ) -> dict[str, Any] | JSONResponse:
        plan = self.plans[plan_id]
        if plan["lifecycle_state"] != "proposed":
            return _error_response(
                "plan_not_approvable", "plan is no longer in the proposed state", 409
            )
        if plan["plan_hash"] != plan_hash:
            return _error_response(
                "plan_hash_mismatch", "plan_hash does not match the proposed plan", 403
            )
        if plan["nonce"] != nonce:
            return _error_response("invalid_nonce", "approval nonce does not match", 403)
        plan["lifecycle_state"] = "approved"
        return {"plan_id": plan_id, "status": "approved"}

    def execute_plan(
        self, plan_id: str, trace_id: str, idempotency_key: str
    ) -> dict[str, Any] | JSONResponse:
        plan = self.plans[plan_id]
        if plan["lifecycle_state"] != "approved":
            return _error_response(
                "plan_not_approved", "plan must be approved before execution", 409
            )
        execution_id = f"exec-{uuid.uuid4().hex[:12]}"
        receipt = {
            "receipt_version": "1.0.0",
            "execution_id": execution_id,
            "plan_id": plan_id,
            "plan_hash": plan["plan_hash"],
            "approval_state": "approved",
            "trace_id": trace_id,
            "idempotency_key": idempotency_key,
            "status": "executed",
            "lifecycle_state": "executed",
        }
        plan["lifecycle_state"] = "executed"
        plan["receipt"] = receipt
        return {"plan_id": plan_id, "lifecycle_state": "executed", "receipt": receipt}


def build_app() -> Starlette:
    """Build a fresh Starlette app with isolated engine state."""
    engine = StubConformanceEngine()

    async def create_decision_case(request: Request) -> JSONResponse:
        body = await request.json()
        return _json_response(engine.create_case(body), status_code=201)

    async def create_analysis_run(request: Request) -> JSONResponse:
        case_id = request.path_params["case_id"]
        try:
            return _json_response(engine.create_run(case_id), status_code=201)
        except KeyError:
            return _error_response("case_not_found", f"case {case_id} not found", 404)

    async def create_action_plan(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        try:
            return _json_response(engine.create_plan(run_id), status_code=201)
        except KeyError:
            return _error_response("run_not_found", f"run {run_id} not found", 404)

    async def get_decision_case(request: Request) -> JSONResponse:
        case_id = request.path_params["case_id"]
        case = engine.cases.get(case_id)
        if case is None:
            return _error_response("case_not_found", f"case {case_id} not found", 404)
        return _json_response(case)

    async def approve_plan(request: Request) -> JSONResponse:
        plan_id = request.path_params["plan_id"]
        if plan_id not in engine.plans:
            return _error_response("plan_not_found", f"plan {plan_id} not found", 404)
        body = await request.json()
        result = engine.approve_plan(plan_id, body.get("plan_hash", ""), body.get("nonce", ""))
        if isinstance(result, JSONResponse):
            return result
        return _json_response(result)

    async def execute_plan(request: Request) -> JSONResponse:
        plan_id = request.path_params["plan_id"]
        if plan_id not in engine.plans:
            return _error_response("plan_not_found", f"plan {plan_id} not found", 404)
        traceparent = request.headers.get("traceparent", "")
        trace_id = (
            traceparent.split("-")[1]
            if traceparent and "-" in traceparent
            else uuid.uuid4().hex * 2
        )
        idempotency_key = request.headers.get("Idempotency-Key") or f"idem-{uuid.uuid4().hex[:12]}"
        result = engine.execute_plan(plan_id, trace_id, idempotency_key)
        if isinstance(result, JSONResponse):
            return result
        return _json_response(result)

    async def export_audit_logs(request: Request) -> Response:
        fmt = request.path_params["fmt"]
        if fmt == "json":
            body = json.dumps([{"event": "audit-entry"}]).encode()
        else:
            body = b"audit-entry\n"
        digest = hashlib.sha256(body).hexdigest()
        return Response(
            body,
            status_code=200,
            headers={
                "Content-Type": "application/json" if fmt == "json" else "text/plain",
                "X-Content-SHA256": digest,
                "X-Audit-Export-Rows": "1",
            },
        )

    return Starlette(
        routes=[
            Route("/v1/decision-cases", create_decision_case, methods=["POST"]),
            Route(
                "/v1/decision-cases/{case_id:str}/analysis-runs",
                create_analysis_run,
                methods=["POST"],
            ),
            Route(
                "/v1/analysis-runs/{run_id:str}/action-plans", create_action_plan, methods=["POST"]
            ),
            Route("/v1/decision-cases/{case_id:str}", get_decision_case, methods=["GET"]),
            Route("/v1/decision-plans/{plan_id:str}/approve", approve_plan, methods=["POST"]),
            Route("/v1/decision-plans/{plan_id:str}/execute", execute_plan, methods=["POST"]),
            Route("/v1/audit-logs/export.{fmt:str}", export_audit_logs, methods=["GET"]),
        ],
    )


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


class StubConformanceServer:
    """Async context manager that runs the conformance stub over a real HTTP socket."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self.base_url: str = ""

    async def __aenter__(self) -> StubConformanceServer:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, port = sock.getsockname()
        self._sock = sock

        app = build_app()
        from uvicorn import Config, Server

        config = Config(app, fd=sock.fileno(), log_level="error")
        server = Server(config)
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
        await _wait_until_serving(port)
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._sock is not None:
            self._sock.close()
