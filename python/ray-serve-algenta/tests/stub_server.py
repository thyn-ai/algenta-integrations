"""A minimal, deterministic, real stub Algenta MCP server for this package's conformance suite --
adapted from `litellm-algenta/tests/stub_server.py`, trimmed to what a byte-transparent proxy's
own test actually needs (this package never calls `execute_decision` or any other contract tool by
name, so none of that stub's governed-execution machinery applies here).

Run with `stateless_http=True`: the one setting that makes this stub an honest stand-in for
Algenta's own real `/mcp` route, whose public endpoint contract states plainly that it implements
"stateless Streamable HTTP" -- verified against the live endpoint's documented behavior during
this package's design, not assumed. A stateful stub would silently make this package's
multi-replica proxy conformance test easier to pass than the real deployment target actually is,
which would defeat the entire point of this package existing.

Nothing here talks to any real Algenta engine -- none is reachable from this test environment.
"""

from __future__ import annotations

import asyncio
import socket

from fastmcp import FastMCP


def build_stub_algenta_server() -> FastMCP:
    """Build a fresh stub server instance with its own isolated in-memory tool set.

    `query_data` deliberately echoes its own arguments back verbatim (`dataset`, `request_id`)
    rather than returning a fixed or aggregated result -- that is what lets a conformance test
    prove a given response was never mixed up with a *different*, concurrently in-flight call
    that happened to land on a different Ray Serve replica, without needing this stub to track any
    cross-call state of its own.
    """
    mcp = FastMCP("algenta-stub")

    @mcp.tool
    def query_data(dataset: str, request_id: str) -> dict:
        return {"dataset": dataset, "request_id": request_id, "rows": [{"value": 1}, {"value": 2}]}

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
    """Async context manager that runs `build_stub_algenta_server()` over a real HTTP socket, in
    genuinely stateless-HTTP mode -- see the module docstring."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self.base_url: str = ""
        self.port: int = 0

    async def __aenter__(self) -> StubServerFixture:
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
                stateless_http=True,
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
