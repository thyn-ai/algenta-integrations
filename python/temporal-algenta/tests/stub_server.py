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
