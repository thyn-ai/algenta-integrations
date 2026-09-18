"""Run `AlgentaMCPProxy` end to end with zero external setup -- no live Algenta engine required.

This starts three things in-process and wires them together exactly like the real deployment
does, just with a stub standing in for your engine:

1. `tests/stub_server.py`'s real `fastmcp.FastMCP` stub -- the same one this package's own
   conformance suite runs against -- on a real HTTP socket.
2. A real two-replica `AlgentaMCPProxy` Ray Serve deployment (`ray_serve_algenta.deployment
   .build_app`), pointed at that stub via `upstream_base_url=` (the same escape hatch
   `ALGENTA_BASE_URL` resolves to in production -- see the package README's "Self-hosted-first"
   section).
3. A real `fastmcp.Client` making one real MCP tool call through the proxy to the stub and back.

Nothing here is mocked: every hop is a genuine HTTP request over a real socket, through the real
`AlgentaMCPProxy` code. Swap the stub for your own self-hosted Algenta engine's `/mcp` URL (see
"Quick start (local)" in the README) once you're ready to move off this walkthrough.

Run it:

    cd python
    uv sync --package ray-serve-algenta --all-extras
    uv run python ray-serve-algenta/examples/try_it_locally.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

# Must be set before `ray` is imported anywhere in this process -- see `tests/conftest.py`'s
# docstring for the full reproduced-live explanation. In short: launched via `uv run` (exactly
# how this script is documented to run), Ray >=2.58's default would otherwise reconstruct the
# same `uv run` invocation for every worker it spawns, resolved against this *workspace's* root
# `pyproject.toml` (deliberately dependency-free) instead of this package's own already-installed
# environment -- every worker then fails with `ModuleNotFoundError: No module named 'ray'`, and
# Ray retries that failure forever instead of surfacing it, so the whole run just appears to hang.
os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

# Makes `tests.stub_server` importable regardless of the current working directory this script
# is launched from -- `tests/` is a sibling of this file's parent directory (the package root),
# not a package this repository publishes, so it is never on `sys.path` by default.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ray  # noqa: E402
from fastmcp import Client  # noqa: E402
from ray import serve  # noqa: E402
from ray_serve_algenta.deployment import build_app  # noqa: E402
from tests.stub_server import StubServerFixture  # noqa: E402

_PROXY_URL = "http://127.0.0.1:8000/mcp"


async def main() -> None:
    async with StubServerFixture() as stub:
        print(f"Stub Algenta engine listening at {stub.base_url} (stands in for your real one)")

        ray.init(num_cpus=4, include_dashboard=False, ignore_reinit_error=True)
        try:
            app = build_app(upstream_base_url=stub.base_url, num_replicas=2)
            serve.run(app, name="ray-serve-algenta-try-it-locally")
            print(f"AlgentaMCPProxy is up at {_PROXY_URL}, forwarding to the stub above\n")

            async with Client(_PROXY_URL) as client:
                result = await client.call_tool(
                    "query_data",
                    {"dataset": "try-it-locally", "request_id": "demo-1"},
                )

            print("Real MCP response, round-tripped through the real proxy:")
            print(result.data)
        finally:
            serve.shutdown()
            ray.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
