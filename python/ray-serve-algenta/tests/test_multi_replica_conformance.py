"""Real conformance tests for `ray_serve_algenta.deployment.AlgentaMCPProxy`.

Every test below runs a genuine `ray.init()` local cluster, a genuine `serve.run(...)` of the real
`AlgentaMCPProxy` deployment (never mocked), in front of a real stub Algenta MCP server
(`tests/stub_server.py`, a real `fastmcp.FastMCP` server on a real HTTP socket, run in
`stateless_http=True` mode to match the real engine's own documented behavior) -- driven over real
HTTP with a real `fastmcp.Client` and real `httpx` requests, never an in-process shortcut. This is
the only way to actually test what this package claims: that Algenta's stateless `/mcp` transport
lets independently-routed Ray Serve replicas behave correctly with no sticky-session requirement.

Heavier than a typical unit-test file by necessity -- `ray.init()`/`serve.run()`/`serve.shutdown()`
/`ray.shutdown()` each take real wall-clock seconds, so (mirroring `litellm-algenta`'s own stated
tradeoff for its real-proxy conformance suite) assertions are bundled into as few full
start/stop cycles as the tests can honestly get away with, rather than one cycle per assertion.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import ray
from fastmcp import Client
from ray import serve

from ray_serve_algenta.deployment import REPLICA_HEADER, AlgentaMCPProxy, build_app

from .stub_server import StubServerFixture

_NUM_REPLICAS = 2
_NUM_CONCURRENT_CALLS = 24
_PROXY_URL = "http://127.0.0.1:8000/mcp"


def test_default_autoscaling_config_has_a_floor_of_at_least_two_replicas() -> None:
    """A fast, no-Ray-runtime-needed regression guard on the class's own decorator config.

    This package's entire README rests on "run with >=2 replicas" -- if a future edit ever
    dropped the shipped default's `min_replicas` below 2 (e.g. while tuning `max_replicas` or
    `target_ongoing_requests`), that would silently invalidate the claim without any of the live
    conformance tests below necessarily catching it (they explicitly pin `num_replicas=2` via
    `build_app(..., num_replicas=2)` for CI speed/determinism -- see that function's own
    docstring -- so they never actually exercise the *default* `autoscaling_config` at all).
    Reads Ray Serve's own internal `_deployment_config` rather than re-deriving the same numbers
    by hand, so this test fails the moment the real config disagrees with what it asserts.
    """
    autoscaling_config = AlgentaMCPProxy._deployment_config.autoscaling_config
    assert autoscaling_config is not None, "the shipped default must use autoscaling_config, not a fixed num_replicas"
    assert autoscaling_config.min_replicas >= 2
    assert autoscaling_config.max_replicas > autoscaling_config.min_replicas


async def test_multi_replica_no_cross_talk_and_replica_diversity() -> None:
    """The two claims this package exists to prove, in one proxy lifecycle:

    1. **No cross-replica state leakage.** `_NUM_CONCURRENT_CALLS` real MCP `query_data` calls,
       each carrying its own unique `request_id`, fired concurrently (interleaved on the wire, not
       sent one at a time) through the proxy. Every response must echo back exactly its own
       request's arguments -- if any replica held or leaked shared mutable state, a fast-enough
       interleaving would eventually surface a response carrying a *different* call's data.
    2. **Real multi-replica routing, not just multi-replica configuration.** The proxy stamps
       every response with `REPLICA_HEADER` naming which Ray Serve replica actually served it
       (see `deployment.py`). Collecting that header across every call and asserting more than one
       distinct value appeared proves requests were actually distributed across >=2 live
       replicas -- a deployment configured for 2 replicas that (due to a routing bug) always
       happened to answer from replica 0 would pass claim 1 above but fail this one.
    """
    async with StubServerFixture() as stub:
        ray.init(num_cpus=4, include_dashboard=False, ignore_reinit_error=True)
        try:
            app = build_app(upstream_base_url=stub.base_url, num_replicas=_NUM_REPLICAS)
            serve.run(app, name="algenta-mcp-proxy-conformance")

            async def call_via_real_mcp_client(i: int) -> tuple[int, str, dict]:
                request_id = str(uuid.uuid4())
                async with Client(_PROXY_URL) as client:
                    result = await client.call_tool(
                        "query_data", {"dataset": f"dataset-{i}", "request_id": request_id}
                    )
                return i, request_id, result.data

            results = await asyncio.gather(
                *[call_via_real_mcp_client(i) for i in range(_NUM_CONCURRENT_CALLS)]
            )
            for i, request_id, data in results:
                assert data["dataset"] == f"dataset-{i}", (
                    f"cross-talk: call {i} got a response carrying dataset {data['dataset']!r}"
                )
                assert data["request_id"] == request_id, f"cross-talk: call {i} got another call's request_id"

            replicas_seen: set[str] = set()
            async with httpx.AsyncClient(timeout=10.0) as client:

                async def raw_call(i: int) -> str | None:
                    resp = await client.post(
                        _PROXY_URL,
                        content=(
                            b'{"jsonrpc":"2.0","id":%d,"method":"tools/list","params":{}}' % i
                        ),
                        headers={
                            "content-type": "application/json",
                            "accept": "application/json, text/event-stream",
                        },
                    )
                    assert resp.status_code == 200
                    return resp.headers.get(REPLICA_HEADER)

                tags = await asyncio.gather(*[raw_call(i) for i in range(30)])
            replicas_seen = {tag for tag in tags if tag}
            assert len(replicas_seen) >= 2, (
                f"expected requests distributed across >={_NUM_REPLICAS} replicas, only saw "
                f"{replicas_seen!r} across 30 calls -- either routing isn't load-balancing, or "
                f"{REPLICA_HEADER} stopped being set"
            )
        finally:
            serve.shutdown()
            ray.shutdown()


async def test_env_var_resolution_without_an_explicit_upstream_arg() -> None:
    """`build_app()` with no `upstream_base_url=` must resolve `ALGENTA_BASE_URL` per-replica --
    the actual operator-facing configuration path (`manifests/rayservice.yaml` sets this env var
    on the container, never passes a Python kwarg). The other tests above always pass
    `upstream_base_url=stub.base_url` explicitly for convenience; this test is the one that
    exercises the real, documented configuration surface end to end, not just the escape hatch.
    """
    async with StubServerFixture() as stub:
        os.environ["ALGENTA_BASE_URL"] = stub.base_url
        ray.init(num_cpus=4, include_dashboard=False, ignore_reinit_error=True)
        try:
            app = build_app(num_replicas=_NUM_REPLICAS)
            serve.run(app, name="algenta-mcp-proxy-env-conformance")
            async with Client(_PROXY_URL) as client:
                result = await client.call_tool("query_data", {"dataset": "env-check", "request_id": "r1"})
            assert result.data == {
                "dataset": "env-check",
                "request_id": "r1",
                "rows": [{"value": 1}, {"value": 2}],
            }
        finally:
            serve.shutdown()
            ray.shutdown()
            del os.environ["ALGENTA_BASE_URL"]
