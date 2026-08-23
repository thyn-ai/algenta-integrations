"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `fastmcp.FastMCP` server, run over a real HTTP socket on `127.0.0.1` -- not an
in-memory transport shortcut and not a mock of anything this package builds. The real subject
under test in this package's conformance suite is a real `litellm --config ...` proxy process
sitting in front of this server, so this stub only needs to be a faithful, deterministic stand-in
for a self-hosted Algenta engine's MCP tool surface -- same governed-execution receipt envelope
and tool set as the `pydantic-ai-algenta` / `langchain-algenta` siblings' own stub servers -- plus
two research-only tools (`echo_trace`, `flaky_after`) that exist purely to observe what the
gateway actually does to a request/response on the wire, mirroring the exact tools the D4 research
phase already built and verified against a real litellm proxy (see that research's
`algenta_stub_server.py`).

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
    """Build a dict shaped exactly like the shared `GovernedExecutionReceipt` field list."""
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


class RevokeAfterNCallsMiddleware:
    """Raw ASGI middleware: after `revoke_after` real `tools/call` requests whose `Authorization`
    header matches `watched_token`, every further such request gets a bare HTTP 401 with no MCP
    framing at all -- simulating an upstream static credential that was revoked mid-session, out
    from under a gateway using a static `auth_type` (`bearer_token`/`api_key`/`basic`/`token`).

    Session-management traffic (initialize, list_tools, notifications, DELETE) is ignored so the
    count matches "N successful tool calls" the way a human would count them. Buffers the request
    body to inspect the JSON-RPC method, then replays it to the real app -- returning a synthetic
    `http.disconnect` instead (an earlier, wrong version of this) abandons the streamable-http SSE
    response mid-write, so after the one buffered replay this delegates to the real `receive()`.
    """

    def __init__(self, app: ASGIApp, *, watched_token: str, revoke_after: int) -> None:
        self.app = app
        self._watched_token = f"Bearer {watched_token}"
        self._revoke_after = revoke_after
        self._seen = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        if auth != self._watched_token or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body_chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)

        is_tool_call = b'"method":"tools/call"' in body or b'"method": "tools/call"' in body

        sent = False

        async def replay_receive() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if is_tool_call:
            self._seen += 1
            if self._seen > self._revoke_after:
                response = JSONResponse({"error": "token_revoked"}, status_code=401)
                await response(scope, receive, send)
                return

        await self.app(scope, replay_receive, send)


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated approval/call-count state.

    Serving it over HTTP with `flaky_after`'s revoke-after-N behavior wired in is
    `StubServerFixture`'s job (it applies `RevokeAfterNCallsMiddleware` when it starts the real
    HTTP listener) -- this function only registers tools, so it stays usable for anything that
    doesn't need that middleware too.
    """
    mcp = FastMCP("algenta-stub")
    approved_plans: set[str] = set()
    call_counts: dict[str, int] = {}

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
    def execute_decision(
        plan_hash: str, idempotency_key: str = "idem-1", execution_id: str | None = None, force: bool = False
    ) -> dict:
        """The safety-critical, approval-gated tool.

        `force` is declared on this fake tool's schema on purpose, mirroring the real contract's
        note that `execute_decision` carries an operator-only `force` field on its real schema --
        this package's `allowed_params` config is what's under test for rejecting it, not this
        server refusing to accept the argument itself.
        """
        exec_id = execution_id or f"exec-{plan_hash}"
        if plan_hash == REJECTED_PLAN_HASH:
            return _receipt(
                status="error",
                code=REJECTED_PLAN_CODE,
                approval_state="rejected",
                plan_hash=plan_hash,
                execution_id=exec_id,
                idempotency_key=idempotency_key,
            )
        if plan_hash in approved_plans:
            return _receipt(
                approval_state="approved",
                plan_hash=plan_hash,
                execution_id=exec_id,
                idempotency_key=idempotency_key,
                result={"executed": True, "plan_hash": plan_hash},
            )
        return _receipt(
            approval_state="pending",
            plan_hash=plan_hash,
            execution_id=exec_id,
            idempotency_key=idempotency_key,
        )

    @mcp.tool
    def _test_approve_plan(plan_hash: str) -> dict:
        """Test-only: stand-in for a human approving the plan via the engine's real HTTP
        endpoint. Not part of the real Algenta MCP tool registry or the tool-profile contract --
        deliberately left out of every real profile's `allowed_tools`, so it's only reachable at
        all through the `full` profile in the conformance tests.
        """
        approved_plans.add(plan_hash)
        return {"approved": True, "plan_hash": plan_hash}

    @mcp.tool
    def echo_trace() -> dict:
        """Return whatever `X-Trace-Id`/`Authorization` headers this request actually carried on
        the wire, so a test can confirm whether the gateway propagated a custom header through to
        this upstream MCP server (and, separately, that the gateway's own HTTP response back to
        the caller does NOT carry anything this tool returns as a response header)."""
        request: Request = get_http_request()
        return {
            "received_headers": {
                "x-trace-id": request.headers.get("x-trace-id"),
                "authorization": request.headers.get("authorization"),
            }
        }

    @mcp.tool
    def flaky_after(key: str) -> dict:
        """Succeeds normally; call-count tracking happens in the ASGI middleware below, which
        returns a raw 401 after `revoke_after` calls bearing `watched_token`. If this tool body
        runs at all, the call got past the middleware -- i.e. this is still a "successful call"."""
        call_counts[key] = call_counts.get(key, 0) + 1
        return _receipt(result={"key": key, "call_count": call_counts[key]})

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

    def __init__(self, *, watched_token: str = "rotating-upstream-token", revoke_after: int = 2) -> None:
        self._watched_token = watched_token
        self._revoke_after = revoke_after
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self.base_url: str = ""
        self.port: int = 0

    async def __aenter__(self) -> "StubServerFixture":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, port = sock.getsockname()
        self._sock = sock
        self.port = port

        mcp = build_stub_algenta_server()
        self._task = asyncio.create_task(
            mcp.run_http_async(
                show_banner=False,
                path="/mcp",
                sockets=[sock],
                middleware=[
                    Middleware(
                        RevokeAfterNCallsMiddleware,
                        watched_token=self._watched_token,
                        revoke_after=self._revoke_after,
                    )
                ],
            )
        )
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
