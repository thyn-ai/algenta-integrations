"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `fastmcp.FastMCP` server, run over a real HTTP socket on `127.0.0.1` -- not an
in-memory transport shortcut and not a mock of anything this package builds. The real subject
under test in this package's conformance suite is a real `litellm --config ...` proxy process
sitting in front of this server, so this stub only needs to be a faithful, deterministic stand-in
for a self-hosted Algenta engine's MCP tool surface, plus two research-only tools (`echo_trace`,
`flaky_after`) that exist purely to observe what the gateway actually does to a request/response on
the wire.

`execute_decision` here mirrors the real engine's tool exactly: it is synchronous, takes
`decision_id`/`webhook_url` (plus optional `timeout_seconds`/`metadata`, and the operator-only
`force`/`override_safety`), and either returns a real `ExecutionReceipt` or raises an exception
carrying one of the three real gate names (`"idempotency"`/`"confidence"`/`"risk_floor"`). There is
no `plan_hash`, no `approval_state`, and no pending/rejected async state anywhere in this file --
an earlier version of this stub modeled one; it was fictional.

Nothing here talks to any real Algenta Engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from litellm_algenta.contract import EXECUTE_DECISION_GATES

#: A decision_id whose confidence is always below `policy.min_confidence` -- `execute_decision`
#: always refuses it with gate `"confidence"` unless `override_safety=true`.
DECISION_ID_LOW_CONFIDENCE = "decision-low-confidence"

#: A decision_id whose `risk_p5` is always below `-policy.risk_floor` -- `execute_decision` always
#: refuses it with gate `"risk_floor"` unless `override_safety=true`.
DECISION_ID_RISK_FLOOR_BREACH = "decision-risk-floor-breach"

#: A tool whose result is intentionally *not* an `ExecutionReceipt`-shaped envelope, to exercise
#: the passthrough path for tools that don't return one (e.g. a real `get_contract` discovery
#: blob).
NON_ENVELOPE_RESULT = {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}

_OVERRIDE_HINTS: dict[str, str] = {
    "idempotency": "Pass force=true to override the idempotency gate for one re-execution.",
    "confidence": "Pass override_safety=true to override the confidence gate.",
    "risk_floor": "Pass override_safety=true to override the risk-floor gate.",
}


class ExecutionBlockedError(Exception):
    """Mirrors the real engine's synchronous `409` `execution_blocked_<gate>` denial exactly:
    `{"error": {"code": "execution_blocked_<gate>", "gate": "<gate>", "message": ...,
    "override_hint": ...}}`.

    Raised from inside the `execute_decision` tool body -- MCP has no independent HTTP status on
    a `tools/call` JSON-RPC result, so a real MCP-fronting `execute_decision` implementation has
    no way to hand a caller a `409` except by surfacing it the same way any other tool-body
    exception surfaces: FastMCP (and any spec-compliant MCP server) turns a raised exception into
    a `tools/call` result with `isError: true`, carrying the exception's message as text content.
    That -- not an async/pending state -- is this tool's one and only failure idiom.
    """

    def __init__(self, gate: str) -> None:
        if gate not in EXECUTE_DECISION_GATES:
            raise ValueError(f"not a real execute_decision gate: {gate!r}")
        self.gate = gate
        self.code = f"execution_blocked_{gate}"
        self.message = f"execute_decision blocked: the {gate!r} gate was not satisfied"
        self.override_hint = _OVERRIDE_HINTS[gate]
        payload = {
            "error": {
                "code": self.code,
                "gate": self.gate,
                "message": self.message,
                "override_hint": self.override_hint,
            }
        }
        super().__init__(json.dumps(payload))


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
    """Build a fresh stub server instance with its own isolated delivery/call-count state.

    Serving it over HTTP with `flaky_after`'s revoke-after-N behavior wired in is
    `StubServerFixture`'s job (it applies `RevokeAfterNCallsMiddleware` when it starts the real
    HTTP listener) -- this function only registers tools, so it stays usable for anything that
    doesn't need that middleware too.
    """
    mcp = FastMCP("algenta-stub")
    delivered_decision_ids: set[str] = set()
    call_counts: dict[str, int] = {}

    @mcp.tool
    def get_contract() -> dict:
        """Fake discovery payload -- deliberately not an ExecutionReceipt-shaped envelope."""
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
        return {"scenario": scenario, "rationale": "looks fine"}

    @mcp.tool
    def log_decision(chosen_action: str) -> dict:
        decision_id = f"decision-{chosen_action}"
        return {
            "decision_id": decision_id,
            "chosen_action": chosen_action,
            "expected_value": 1.0,
            "confidence": 0.9,
            "created_at": "2026-08-23T00:00:00Z",
            "note": None,
        }

    @mcp.tool
    def execute_decision(
        decision_id: str,
        webhook_url: str,
        timeout_seconds: int | None = None,
        metadata: dict | None = None,
        force: bool = False,
        override_safety: bool = False,
    ) -> dict:
        """The safety-critical tool -- synchronous, no approval-pause, no plan_hash.

        `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
        the real engine's schema carrying an operator-only `force`/`override_safety` field on
        `execute_decision` -- this package's `allowed_params` config is what's under test for
        rejecting them before they ever reach here, not this server refusing the argument itself.

        Gate behavior (matching the real engine exactly):
        - `decision_id == DECISION_ID_LOW_CONFIDENCE`: always the `"confidence"` gate, unless
          `override_safety=True`.
        - `decision_id == DECISION_ID_RISK_FLOOR_BREACH`: always the `"risk_floor"` gate, unless
          `override_safety=True`.
        - Any other `decision_id` that has already been delivered once by this server instance:
          the `"idempotency"` gate, unless `force=True` (one re-execution).
        - Otherwise: a real 200 `ExecutionReceipt`, and the decision_id is marked delivered.
        """
        if decision_id == DECISION_ID_LOW_CONFIDENCE and not override_safety:
            raise ExecutionBlockedError("confidence")
        if decision_id == DECISION_ID_RISK_FLOOR_BREACH and not override_safety:
            raise ExecutionBlockedError("risk_floor")
        if decision_id in delivered_decision_ids and not force:
            raise ExecutionBlockedError("idempotency")

        delivered_decision_ids.add(decision_id)
        return {
            "decision_id": decision_id,
            "webhook_url": webhook_url,
            "execution_status": "delivered",
            "response_code": 200,
            "executed_at": "2026-08-23T00:00:00Z",
            "policy_snapshot_id": "policy-snap-1",
            "schema_snapshot_id": "schema-snap-1",
            "manifest_version": "1.0.0",
            "payload_summary": {"decision_id": decision_id, "timeout_seconds": timeout_seconds, "metadata": metadata},
            "safety_overridden": bool(force or override_safety),
        }

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
        return {"key": key, "call_count": call_counts[key]}

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
