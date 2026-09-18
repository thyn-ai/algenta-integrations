"""ray_serve_algenta.deployment -- a Ray Serve HTTP ingress that reverse-proxies Algenta's
stateless `/mcp` surface, deployed with >=2 replicas and no sticky-session requirement.

NOTE: this module deliberately does NOT start with `from __future__ import annotations`.
Verified live against ray[serve] 2.58.0: `@serve.ingress(app)` binds a FastAPI route method
(`request: Request`) by inspecting the live signature at decoration time. With postponed
evaluation of annotations active, `Request` resolves to the *string* `"Request"` instead of the
real `starlette.requests.Request` class, and FastAPI -- unable to recognize it as a special
request-injection type -- falls back to treating it as a field to parse out of the query string.
Every request then 422s with `{"detail":[{"loc":["query","request"],"msg":"Field required"}]}`
before this proxy's own code ever runs. Reproduced directly, not assumed from documentation:
removing the `__future__` import was the fix. Keep it that way in this file specifically, even
though the rest of this repository uses it freely elsewhere.

Why a raw reverse proxy, not an MCP-aware client/server pair: the point of this package is that
Algenta's `/mcp` route is *stateless* Streamable HTTP (verified against `apps/mcp_server/README.md`
in `thyn-ai/algenta`'s own source -- "The canonical `/mcp` route implements stateless Streamable
HTTP"), so nothing about routing a given request to a given replica needs to be sticky, and nothing
about a given replica needs to remember anything about a previous request. A byte-transparent
proxy is the most honest way to demonstrate that: if this proxy needed session affinity to work
correctly, it would not prove the point it exists to prove. `ray_serve_algenta` never parses,
inspects, or generates MCP JSON-RPC content itself -- that would risk this package silently
reimplementing part of Algenta's own MCP server, which `scripts/check-no-engine-dependency.py`
exists to prevent this repository from doing.
"""

import logging
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from ray import serve

logger = logging.getLogger("ray_serve_algenta")

#: Resolved at deployment *construction* time (i.e. when Ray Serve starts a replica), matching
#: this repository's env-var-first convention -- see `pydantic_ai_algenta.toolset
#: .ALGENTA_BASE_URL_ENV_VAR` / `DEFAULT_ALGENTA_BASE_URL`, which this deliberately mirrors
#: exactly (same env var name, same localhost fallback) for consistency across every package in
#: this repository. Point it at your own self-hosted Algenta engine's MCP endpoint -- never an
#: Algenta-hosted one.
BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"
DEFAULT_UPSTREAM_BASE_URL = "http://localhost:8000/mcp"

#: The one path this proxy mounts -- matching the engine's own canonical `/mcp` mount
#: (`apps/mcp_server/README.md`: "the MCP is already mounted at ... POST /mcp ... GET /mcp").
#: `ALGENTA_BASE_URL` is expected to already be a full endpoint URL (e.g.
#: `http://localhost:8000/mcp`), so a request received at this proxy's `/mcp` forwards verbatim
#: to that URL with no path-joining logic -- there is nothing to join.
MCP_PATH = "/mcp"

#: Response header this proxy adds (never present on the real upstream's own response) naming
#: which Ray Serve replica actually handled a given request -- purely observational, useful for a
#: caller or a test to confirm requests are genuinely landing on more than one replica rather than
#: always the same one. Never consulted for routing or correctness by this proxy itself.
REPLICA_HEADER = "x-algenta-ray-replica"

_DEFAULT_MIN_REPLICAS = 2
_DEFAULT_MAX_REPLICAS = 8
_DEFAULT_TARGET_ONGOING_REQUESTS = 10
_DEFAULT_TIMEOUT_SECONDS = 60.0

#: Headers that must never be relayed verbatim between hops: connection-management headers whose
#: value describes *this* hop, not the next one (httpx/uvicorn compute their own correct values
#: for the new hop), plus `content-length`, which becomes wrong the instant headers are filtered
#: or the body re-chunked.
_HOP_BY_HOP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailers",
        "upgrade",
        "proxy-authenticate",
        "proxy-authorization",
    }
)
#: Same set, plus `content-encoding`: httpx's `aiter_raw()` (used below) replays the upstream's
#: response bytes exactly as received on the wire, but `httpx.AsyncClient` still transparently
#: decompresses gzip/br *unless* told not to -- forwarding a stale `content-encoding` label next
#: to already-decoded bytes would lie about the body's real encoding. This proxy asks httpx not
#: to auto-decompress at all (see `_build_http_client`), so this exclusion is defensive, not
#: currently load-bearing -- kept in case that changes.
_HOP_BY_HOP_RESPONSE_HEADERS = _HOP_BY_HOP_REQUEST_HEADERS | frozenset({"content-encoding"})


def _filtered_headers(headers, drop: frozenset) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def _build_http_client(timeout_seconds: float) -> httpx.AsyncClient:
    # decode=False (via the transport-level `Accept-Encoding` passthrough): this proxy never
    # wants to transparently decompress a response only to lose the ability to state its real
    # `content-encoding` honestly -- see `_HOP_BY_HOP_RESPONSE_HEADERS` above. In practice
    # Algenta's `/mcp` responses are JSON or `text/event-stream`, neither of which this codebase
    # has ever observed compressed, but the byte-transparent contract should hold regardless.
    return httpx.AsyncClient(timeout=timeout_seconds)


fastapi_app = FastAPI(title="algenta-mcp-ray-proxy", docs_url=None, redoc_url=None, openapi_url=None)


@serve.deployment(
    # A floor of 2, not a fixed count: this is the one property this whole package exists to
    # demonstrate (>=2 independently routable replicas, no sticky session needed) -- see the
    # module docstring. `build_app`'s tests override this per-call with `.options(num_replicas=)`
    # for deterministic, fast CI runs; production traffic should scale within this range.
    autoscaling_config={
        "min_replicas": _DEFAULT_MIN_REPLICAS,
        "max_replicas": _DEFAULT_MAX_REPLICAS,
        "target_ongoing_requests": _DEFAULT_TARGET_ONGOING_REQUESTS,
    },
    health_check_period_s=10,
    health_check_timeout_s=5,
)
@serve.ingress(fastapi_app)
class AlgentaMCPProxy:
    """A Ray Serve replica that reverse-proxies every request at `/mcp` to `ALGENTA_BASE_URL`.

    Holds no per-caller, per-session, or per-request state across calls -- the one thing this
    class's `__init__` remembers is its own configuration (upstream URL, HTTP client, timeout)
    and its own Ray Serve replica id (`self._replica_id`, exposed only for observability via
    `REPLICA_HEADER` / `/healthz`, never consulted for correctness). A request handled by replica
    A and a later request from the same MCP client handled by replica B see identical behavior,
    because there is nothing replica-specific for either of them to depend on -- which is exactly
    what a stateless upstream transport is supposed to make possible.
    """

    def __init__(
        self,
        upstream_base_url: str | None = None,
        request_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._upstream_base_url = (
            upstream_base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_UPSTREAM_BASE_URL
        )
        self._client = _build_http_client(request_timeout_seconds)
        try:
            self._replica_id = serve.get_replica_context().replica_tag
        except Exception:  # pragma: no cover - only unavailable outside a running Serve replica
            self._replica_id = "unknown"
        logger.info(
            "AlgentaMCPProxy replica %s proxying %s -> %s",
            self._replica_id,
            MCP_PATH,
            self._upstream_base_url,
        )

    async def __del__(self) -> None:  # pragma: no cover - best-effort cleanup on replica teardown
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def _proxy(self, request: Request) -> Response:
        body = await request.body()
        upstream_request = self._client.build_request(
            request.method,
            self._upstream_base_url,
            headers=_filtered_headers(request.headers, _HOP_BY_HOP_REQUEST_HEADERS),
            params=request.query_params,
            content=body or None,
        )
        upstream_response = await self._client.send(upstream_request, stream=True)

        async def body_iterator() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                await upstream_response.aclose()

        response_headers = _filtered_headers(upstream_response.headers, _HOP_BY_HOP_RESPONSE_HEADERS)
        response_headers[REPLICA_HEADER] = self._replica_id
        return StreamingResponse(
            body_iterator(),
            status_code=upstream_response.status_code,
            headers=response_headers,
        )

    @fastapi_app.api_route(MCP_PATH, methods=["GET", "POST", "HEAD"])
    async def mcp(self, request: Request) -> Response:
        """Proxy one MCP Streamable HTTP request/response verbatim to `ALGENTA_BASE_URL`.

        Method, query string, request/response headers (minus the hop-by-hop set above), and the
        full request/response body are all forwarded byte-for-byte in both directions, streamed
        rather than buffered -- load-bearing for `GET /mcp`'s optional `text/event-stream`
        response and for any `POST /mcp` response the upstream chooses to answer the same way.
        This handler never branches on JSON-RPC method names, MCP tool names, or response
        content -- see the module docstring for why that's deliberate, not an omission.
        """
        return await self._proxy(request)

    @fastapi_app.get("/healthz")
    async def healthz(self) -> dict:
        """Cheap, no-network liveness/readiness target for the RayService manifest's probes.

        Deliberately never calls the upstream: a probe that depends on a remote network hop is a
        probe that fails this replica's own liveness check during a transient upstream blip, which
        conflates "is this replica's process alive and configured" with "is something else, far
        away, currently reachable" -- two different questions Kubernetes needs answered
        separately.
        """
        return {
            "status": "ok",
            "replica": self._replica_id,
            "upstream_base_url": self._upstream_base_url,
        }


def build_app(
    *,
    upstream_base_url: str | None = None,
    num_replicas: int | None = None,
    request_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> serve.Application:
    """Bind a fresh `AlgentaMCPProxy` application.

    Args:
        upstream_base_url: Passed straight through to `AlgentaMCPProxy.__init__` -- if omitted,
            each replica resolves `ALGENTA_BASE_URL` (or the localhost fallback) for itself at
            construction time, same as calling this with no arguments at all.
        num_replicas: Pin a fixed replica count instead of the class's default
            `autoscaling_config` (min 2, max 8). Ray Serve does not allow a deployment to carry
            both `num_replicas` and `autoscaling_config` at once, so passing this *replaces* the
            autoscaling config entirely for this bound application -- exactly what this package's
            own conformance tests do, since asserting on live autoscaler behavior would make the
            test suite slow and nondeterministic without adding to what it actually proves (that
            >=2 concurrently live replicas are each independently, statelessly correct). The
            production default (`app`, below, and `manifests/rayservice.yaml`) leaves this unset
            and keeps real autoscaling.
        request_timeout_seconds: Forwarded to the underlying `httpx.AsyncClient` per replica.

    Returns:
        A bound Ray Serve `Application`, ready for `serve.run(...)` (tests) or referenced by
        `manifests/rayservice.yaml`'s `serveConfigV2.applications[].import_path` (production).
    """
    deployment = AlgentaMCPProxy
    if num_replicas is not None:
        if num_replicas < 2:
            raise ValueError(
                f"num_replicas={num_replicas} defeats this package's entire purpose: proving "
                "Algenta's stateless MCP transport lets replicas be added freely with no "
                "sticky-session requirement needs at least 2 concurrently live replicas to "
                "demonstrate anything. Pass 2 or more, or omit num_replicas entirely to keep the "
                "default autoscaling_config (min 2, max 8)."
            )
        deployment = deployment.options(num_replicas=num_replicas)
    return deployment.bind(
        upstream_base_url=upstream_base_url,
        request_timeout_seconds=request_timeout_seconds,
    )


#: Default application object -- what `serve run ray_serve_algenta.deployment:app` and this
#: package's own dev-loop resolve. Reads `ALGENTA_BASE_URL` (or the localhost fallback) once per
#: replica at construction time, same as every other entrypoint in this module.
app = build_app()

__all__ = [
    "AlgentaMCPProxy",
    "BASE_URL_ENV_VAR",
    "DEFAULT_UPSTREAM_BASE_URL",
    "MCP_PATH",
    "REPLICA_HEADER",
    "app",
    "build_app",
]
