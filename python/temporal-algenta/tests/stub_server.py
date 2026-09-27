"""The test suite's stub Algenta Engine: one isolated `DemoAlgentaEngine` per test, served over
a real HTTP socket, plus one test-only diagnostics tool.

The engine implementation itself lives in `recipes/demo_engine.py` -- the *same* deterministic
FastMCP server the recipes' standalone `main()` runners use, so the wire shape the tests
verify is byte-for-byte the wire shape a first-time recipe run exercises (one implementation,
no drift). This file is the house-pattern fixture around it (mirroring
`python/langchain-algenta/tests/stub_server.py`): a per-test async context manager yielding a
real `127.0.0.1` base URL.

`_test_diagnostics` stands in for the wider admin/ops tooling a real engine's MCP registry
advertises beyond the four contract profiles' named tools -- it exists only to prove that
`profile="full"` genuinely exposes tools the other three profiles never list (see
`tests/test_profile_enforcement.py`).
"""

from __future__ import annotations

import asyncio
import threading
from types import TracebackType

# Re-export the shared constants under the house-pattern import path, mirroring how sibling
# packages' tests import them from `tests.stub_server`.
from recipes.demo_engine import (  # noqa: F401
    AUDIT_DATASET,
    BELOW_RISK_FLOOR_DECISION_ID,
    CONFIDENCE_GATE_CODE,
    FAILED_DELIVERY_DECISION_ID,
    FLAKY_DATASET,
    IDEMPOTENCY_GATE_CODE,
    LOW_CONFIDENCE_DECISION_ID,
    RISK_FLOOR_GATE_CODE,
    DemoAlgentaEngine,
    serve_demo_engine,
)


class StubServerFixture:
    """Async context manager serving one fresh `DemoAlgentaEngine` on an ephemeral port.

    Yields itself: `.base_url` for `AlgentaActivities(base_url=...)` and `.engine` for
    asserting on what the engine actually did (delivered ids, logged decisions, per-tool
    attempt counts).
    """

    def __init__(self) -> None:
        self.engine = DemoAlgentaEngine()
        self.base_url = ""
        self._cm = None

    async def __aenter__(self) -> StubServerFixture:
        @self.engine.server.tool()
        def _test_diagnostics() -> dict:
            """Test-only: not part of the real tool-profile contract at all."""
            return {"delivered_count": len(self.engine.delivered_decision_ids)}

        self._cm = serve_demo_engine(self.engine)
        self.base_url, _ = await self._cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        assert self._cm is not None
        await self._cm.__aexit__(exc_type, exc_val, exc_tb)


class ThreadedEngineServer:
    """Serve one `DemoAlgentaEngine` on a dedicated thread with its own event loop.

    Recipe tests run several sequential servers per pytest process (one per test file), and
    each test file's loop also hosts a Temporal dev-server client and per-test workers. On a
    shared loop a slow graceful shutdown forces `serve_demo_engine`'s cancel fallback, which
    strands FastMCP's lifespan half-shut -- and the NEXT server started on that loop then
    accepts connections but never completes responses (reproduced in CI and in local Linux
    containers as `initialize()` read timeouts plus uvicorn's "ASGI callable returned without
    completing response"). Giving every server its own thread and loop removes the whole
    class: sequential servers never share a loop, and the graceful shutdown never races
    test-loop or Temporal teardown.

    The engine stays in-process, so tests keep asserting on it directly
    (`engine.delivered_decision_ids`, `engine.reset()`, ...). The serving thread is a daemon:
    a truly wedged shutdown can never hang the suite -- worst case the thread is discarded at
    process exit. All waits are bounded and surface as loud errors.
    """

    #: Bounded startup wait: covers a cold loop + uvicorn + FastMCP lifespan on a loaded box.
    START_TIMEOUT = 30.0
    #: Bounded shutdown wait: graceful uvicorn/FastMCP unwind on the server's own loop.
    STOP_TIMEOUT = 30.0

    def __init__(self) -> None:
        self.engine = DemoAlgentaEngine()
        self.base_url = ""
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="stub-engine-server", daemon=True)

    def _run(self) -> None:
        async def amain() -> None:
            @self.engine.server.tool()
            def _test_diagnostics() -> dict:
                """Test-only: not part of the real tool-profile contract at all."""
                return {"delivered_count": len(self.engine.delivered_decision_ids)}

            async with serve_demo_engine(self.engine) as (base_url, _):
                self.base_url = base_url
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.05)

        try:
            asyncio.run(amain())
        except BaseException as error:  # surfaced by start(); never swallowed
            self._error = error
            self._ready.set()

    def start(self) -> ThreadedEngineServer:
        """Start the server thread and wait for it to serve (lifespan included)."""
        self._thread.start()
        if not self._ready.wait(timeout=self.START_TIMEOUT):
            raise TimeoutError(
                f"stub engine server thread did not report serving within {self.START_TIMEOUT}s"
            )
        if self._error is not None:
            raise RuntimeError("stub engine server thread failed during startup") from self._error
        return self

    def stop(self) -> None:
        """Ask the server to shut down gracefully; join the thread, bounded."""
        self._stop.set()
        self._thread.join(timeout=self.STOP_TIMEOUT)
        if self._thread.is_alive():
            # Loud, not silent: a wedged graceful shutdown is a real bug to surface. The daemon
            # thread is left to be reaped at process exit; it cannot hang the suite.
            raise TimeoutError(
                f"stub engine server thread did not stop within {self.STOP_TIMEOUT}s"
            )
        if self._error is not None:
            raise RuntimeError("stub engine server thread failed while serving") from self._error
