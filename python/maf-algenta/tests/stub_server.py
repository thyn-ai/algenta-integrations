"""A minimal, deterministic, fake self-hosted Algenta MCP server for tests.

This is a real `mcp.server.fastmcp.FastMCP` server (the base MCP SDK's own FastMCP -- already a
transitive dependency of `agent-framework`'s MCP client support, so no extra `fastmcp` package is
needed), run over a real HTTP socket on `127.0.0.1` -- not an in-memory transport shortcut and not
a mock of `maf_algenta.toolset`'s internals. Tests exercise the real `maf_algenta.create_algenta_tools`
-> real `agent_framework.MCPStreamableHTTPTool` -> real `mcp` client -> real wire -> this server
round trip, so a wire-shape regression (e.g. the receipt envelope not surviving the MCP text-content
round trip) would actually be caught here, unlike a test that mocks tool execution directly.

`execute_decision` here is shaped exactly like the real, verified engine contract: it either
returns a real `ExecutionReceipt`-shaped dict (HTTP 200 equivalent) or raises a plain exception
whose message is the real, JSON-encoded `{"error": {"code": "execution_blocked_<gate>", ...}}`
409 body -- `mcp.server.lowlevel.server`'s own `call_tool` handler (confirmed by reading the
installed `mcp` SDK source directly) turns *any* exception raised inside a `@mcp.tool()` function
into exactly that `isError=True` / `str(exc)`-as-text shape, which is exactly how the real engine's
own MCP tool wrapper would surface a real HTTP 409 it caught internally. No plan_hash, no
approval_state, no pending state -- none of that exists on the real tool, so none of it is
modeled here either.

Identical in spirit (same fake-server-over-a-real-socket technique) to its siblings in
`python/pydantic-ai-algenta/tests/stub_server.py` and `python/langchain-algenta/tests/stub_server.py`,
once each is independently corrected against the same real facts -- deliberately not reinvented
per package, so a behavioral difference across the three Python integrations would show up as a
real test divergence, not get lost in three different fixture shapes.

Nothing here talks to any real Algenta engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

from mcp.server.fastmcp import FastMCP

#: A `decision_id` `execute_decision` always blocks on the real `"confidence"` gate for, unless
#: the call carries `override_safety=True` -- simulates a decision logged with a confidence below
#: `policy.min_confidence`.
LOW_CONFIDENCE_DECISION_ID = "decision-low-confidence"

#: A `decision_id` `execute_decision` always blocks on the real `"risk_floor"` gate for, unless
#: the call carries `override_safety=True` -- simulates a decision logged with `risk_p5` below
#: `-policy.risk_floor`.
LOW_RISK_FLOOR_DECISION_ID = "decision-low-risk-floor"

#: A `decision_id` `execute_decision` succeeds (no 409) for, but with a payload missing a
#: required `ExecutionReceipt` field -- exercises `AlgentaToolExecutionFailed`'s "the call
#: completed without error but the payload doesn't validate as a real receipt" anomaly path.
MALFORMED_RECEIPT_DECISION_ID = "decision-malformed-receipt"

#: A tool whose result is intentionally *not* an `ExecutionReceipt`-shaped envelope at all, to
#: exercise the passthrough path for tools that don't return one (e.g. a real `get_contract`
#: discovery blob).
NON_ENVELOPE_RESULT = {"capabilities": ["query", "simulate", "recommend"], "engine_version": "1.4.0"}


def _blocked(*, gate: str, message: str, override_hint: str) -> dict[str, Any]:
    """Build the real 409 error body `execute_decision` sends for a named policy-gate denial."""
    return {
        "error": {
            "code": f"execution_blocked_{gate}",
            "gate": gate,
            "message": message,
            "override_hint": override_hint,
        }
    }


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated delivery state."""
    mcp = FastMCP("algenta-stub")
    delivered_decisions: set[str] = set()

    @mcp.tool()
    def get_contract() -> dict:
        """Fake discovery payload -- deliberately not an `ExecutionReceipt`-shaped envelope."""
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
        return {"scenario": scenario, "rationale": "looks fine"}

    @mcp.tool()
    def log_decision(chosen_action: str, expected_value: float = 0.0, confidence: float = 0.9) -> dict:
        decision_id = f"decision-{chosen_action}"
        return {
            "decision_id": decision_id,
            "chosen_action": chosen_action,
            "expected_value": expected_value,
            "confidence": confidence,
            "created_at": "2026-08-23T00:00:00+00:00",
            "note": "logged",
        }

    @mcp.tool()
    def execute_decision(
        decision_id: str,
        webhook_url: str,
        timeout_seconds: int = 30,
        force: bool = False,
        override_safety: bool = False,
    ) -> dict:
        """The real, safety-critical tool: a real `ExecutionReceipt` (success) or a raised,
        real synchronous 409 (one of the three real named gates) -- never anything else.

        `force`/`override_safety` are declared on this fake tool's schema on purpose, mirroring
        the real contract's note that they're operator-only fields on the real schema -- the
        package under test is responsible for stripping/scrubbing them, not this server.
        """
        if decision_id in delivered_decisions and not force:
            raise ValueError(
                json.dumps(
                    _blocked(
                        gate="idempotency",
                        message=f"decision {decision_id!r} has already been delivered.",
                        override_hint="Set force=true to allow one re-execution.",
                    )
                )
            )
        if decision_id == LOW_CONFIDENCE_DECISION_ID and not override_safety:
            raise ValueError(
                json.dumps(
                    _blocked(
                        gate="confidence",
                        message="decision confidence 0.41 is below policy.min_confidence 0.60.",
                        override_hint="Set override_safety=true to bypass the confidence gate.",
                    )
                )
            )
        if decision_id == LOW_RISK_FLOOR_DECISION_ID and not override_safety:
            raise ValueError(
                json.dumps(
                    _blocked(
                        gate="risk_floor",
                        message="risk_p5 -0.42 is below policy.risk_floor -0.25.",
                        override_hint="Set override_safety=true to bypass the risk floor gate.",
                    )
                )
            )

        if decision_id == MALFORMED_RECEIPT_DECISION_ID:
            # A real bug on the engine side (or a version skew this package hasn't caught up
            # with) would look like this: HTTP 200, but the body isn't a real receipt --
            # missing `execution_status` entirely here.
            return {"decision_id": decision_id, "webhook_url": webhook_url}

        delivered_decisions.add(decision_id)
        return {
            "decision_id": decision_id,
            "webhook_url": webhook_url,
            "execution_status": "delivered",
            "response_code": 200,
            "executed_at": "2026-08-23T00:00:00+00:00",
            "policy_snapshot_id": "policy-snap-1",
            "schema_snapshot_id": "schema-snap-1",
            "manifest_version": "1",
            "payload_summary": {"decision_id": decision_id, "timeout_seconds": timeout_seconds},
            "safety_overridden": override_safety,
        }

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
        self._server: Any = None
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
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
        await _wait_until_serving(port)
        self.base_url = f"http://127.0.0.1:{port}/mcp"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._task is not None
        # `server.should_exit = True` asks uvicorn's own serve loop to return on its next
        # iteration -- a clean shutdown ASGI `lifespan.shutdown` sees coming, unlike
        # `task.cancel()`, which interrupts uvicorn's lifespan handling mid-`await` and makes it
        # log a spurious `CancelledError` traceback on every teardown (harmless, but exactly the
        # kind of noise a real end user running this fixture themselves -- see the README's "Try
        # it locally" section -- shouldn't have to explain away). Falls back to a hard cancel if
        # the server doesn't exit promptly, so teardown still can't hang.
        if self._server is not None:
            self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
        if self._sock is not None:
            self._sock.close()
