"""In-process tests for `ray_serve_algenta.deployment` -- no Ray cluster, no Serve runtime.

`test_multi_replica_conformance.py` runs the real deployment on a real local `ray.init()` cluster,
which is the only honest way to prove this package's multi-replica claims. But every line a replica
executes *per request* -- `AlgentaMCPProxy.__init__`, `_proxy`, `mcp`, `healthz` and the two module
helpers -- runs inside Ray's replica worker processes, which coverage.py in the pytest driver
process never observes: measured 2026-09-19, all of them reported uncovered despite being exercised
end to end on every CI run. This module drives the same handlers in this process, so that behavior
is asserted at the byte level rather than inferred from "the cluster answered 200":

* `AlgentaMCPProxy` (as imported) is Ray Serve's `Deployment` handle around an `@serve.ingress`
  wrapper class whose `__init__` is a coroutine Ray awaits at replica start. The plain class this
  package actually defines is the next entry in that wrapper's MRO (see `_user_class`), and it
  constructs normally outside a replica: `serve.get_replica_context()` raises there, which
  `__init__` already handles by reporting the replica as `"unknown"`.
* Requests are real `starlette.requests.Request` objects built from ASGI scopes, and responses are
  drained through the ASGI `send` callable -- status and headers are asserted exactly as they
  would reach uvicorn, not as a Python dict the handler happened to build.
* The upstream is always a real HTTP server on a real loopback socket, never a patched client:
  either this suite's existing stub Algenta MCP server (`stub_server.py`: fastmcp, stateless mode)
  or `_RecordingUpstream`, a uvicorn-served ASGI app that records exactly what the proxy put on
  the wire and answers with whatever the test scripted.
"""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import socket
import zlib
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import ray_serve_algenta
import uvicorn
from fastapi import Response
from ray import serve
from ray_serve_algenta import deployment
from ray_serve_algenta.deployment import (
    BASE_URL_ENV_VAR,
    DEFAULT_UPSTREAM_BASE_URL,
    MCP_PATH,
    REPLICA_HEADER,
    AlgentaMCPProxy,
    build_app,
)
from starlette.requests import Request

from .stub_server import StubServerFixture, _wait_until_serving

# Outside a Ray Serve replica nothing awaits the class's `async def __del__`. Ray's replica does so
# explicitly on graceful shutdown (`ray.serve._private.replica.Replica.call_destructor`, which
# accepts an async destructor by design), and the `make_proxy` fixture below mirrors that. CPython's
# own finalizer then calls `__del__` a second time at garbage collection, receives a coroutine it
# cannot await, and warns -- an artifact of instantiating a Serve deployment class in a plain
# process, not a leak (the fixture already closed the client), so that one warning is filtered here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:coroutine 'AlgentaMCPProxy.__del__' was never awaited:RuntimeWarning"
)

#: The connection-specific headers a proxy must never relay between hops (RFC 9110 section 7.6.1's
#: set, plus `content-length`, which stops being true once a body is re-streamed) -- the contract
#: `deployment.py` documents for both directions. Spelled out here, independently of the module's
#: own constant, so a regression in that constant fails this suite instead of being mirrored by it.
_NEVER_RELAYED = frozenset(
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

_TOOLS_LIST = b'{"jsonrpc":"2.0","id":7,"method":"tools/list","params":{}}'
_MCP_REQUEST_HEADERS = (
    ("content-type", "application/json"),
    ("accept", "application/json, text/event-stream"),
)


def _user_class() -> type:
    """The plain Python class `deployment.py` defines, unwrapped from Ray Serve's decorators.

    `AlgentaMCPProxy.func_or_class` is `@serve.ingress`'s wrapper subclass, whose `__init__` is a
    coroutine Ray awaits during replica construction; Ray gives that wrapper the wrapped class's
    name and module, so the MRO is walked for the first entry with a plain synchronous `__init__`
    -- the class whose methods these tests exercise.
    """
    wrapper = AlgentaMCPProxy.func_or_class
    for candidate in wrapper.__mro__:
        if candidate.__module__ == deployment.__name__ and not inspect.iscoroutinefunction(
            candidate.__init__
        ):
            return candidate
    raise AssertionError(f"no synchronous-__init__ class from {deployment.__name__} in {wrapper.__mro__!r}")


@pytest.fixture
async def make_proxy() -> AsyncIterator[Callable[..., Any]]:
    """Constructs `AlgentaMCPProxy` instances in this process and finalizes each the way Ray Serve
    does on graceful replica shutdown -- by awaiting its `async def __del__`, which is what
    actually closes the replica's `httpx.AsyncClient` (see the module docstring)."""
    created: list[Any] = []

    def _make(**kwargs: Any) -> Any:
        proxy = _user_class()(**kwargs)
        created.append(proxy)
        return proxy

    yield _make
    for proxy in created:
        await proxy.__del__()


def _build_request(
    method: str,
    *,
    query: bytes = b"",
    headers: Iterable[tuple[str, str]] = (),
    body: bytes = b"",
) -> Request:
    """A real Starlette `Request` for `MCP_PATH`, from a hand-built ASGI HTTP scope."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": MCP_PATH,
        "raw_path": MCP_PATH.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
        "client": ("127.0.0.1", 40000),
        "server": ("127.0.0.1", 8000),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _Sent:
    """What a response put on the ASGI `send` channel: status and headers exactly as uvicorn
    would receive them, plus the body chunks in arrival order. `headers` is a plain dict, which
    is only lossless while every header name occurs once -- `_drain` asserts exactly that, so a
    repeated name (legal HTTP, e.g. `set-cookie`) fails the test instead of silently keeping
    whichever copy came last."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


async def _drain(response: Response, *, on_chunk: Callable[[bytes], None] | None = None) -> _Sent:
    sent = _Sent()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            sent.status = message["status"]
            pairs = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message["headers"]]
            sent.headers = dict(pairs)
            assert len(sent.headers) == len(pairs), f"repeated response header name in {pairs!r}"
        elif message["type"] == "http.response.body" and message.get("body"):
            sent.chunks.append(message["body"])
            if on_chunk is not None:
                on_chunk(message["body"])

    async def receive() -> dict[str, Any]:
        raise AssertionError("a response must never read from the request channel")

    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    return sent


@dataclass
class _ReceivedRequest:
    method: str
    query: bytes
    headers: dict[str, str]
    body: bytes


@dataclass
class _RecordingUpstream:
    """A real HTTP upstream on a loopback socket -- uvicorn, the same server the stub engine runs
    on -- that records every request it receives and answers with the scripted status, headers
    and body chunks. Each chunk may be gated on an `asyncio.Event` the test controls, and the
    whole reply may be delayed, so streaming and timeout behavior can be asserted deterministically
    rather than by timing. Not a patched client: the proxy's own `_build_http_client` talks to
    this over TCP exactly as it would to the engine."""

    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    chunks: list[bytes | tuple[asyncio.Event, bytes]] = field(default_factory=list)
    delay_seconds: float = 0.0
    requests: list[_ReceivedRequest] = field(default_factory=list)
    base_url: str = ""
    port: int = 0

    async def __aenter__(self) -> _RecordingUpstream:
        async def asgi(scope: dict[str, Any], receive: Callable[[], Any], send: Callable[[Any], Any]) -> None:
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            self.requests.append(
                _ReceivedRequest(
                    method=scope["method"],
                    query=scope["query_string"],
                    headers={k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]},
                    body=body,
                )
            )
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status,
                    "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in self.headers],
                }
            )
            for chunk in self.chunks:
                if isinstance(chunk, tuple):
                    gate, chunk = chunk
                    await gate.wait()
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(100)
        _, self.port = sock.getsockname()
        self._sock = sock
        server = uvicorn.Server(uvicorn.Config(asgi, lifespan="off", log_level="warning", access_log=False))
        self._task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await _wait_until_serving(self.port)
        except BaseException:
            # `async with` never reaches `__aexit__` when `__aenter__` raises, so a server that
            # failed to come up (the readiness timeout, a cancelled test) must be torn down here
            # or its task and socket outlive the test.
            await self.__aexit__(None, None, None)
            raise
        self.base_url = f"http://127.0.0.1:{self.port}/mcp"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._sock.close()


async def test_replica_construction_resolves_the_upstream_url_arg_then_env_then_default(
    monkeypatch: pytest.MonkeyPatch, make_proxy: Callable[..., Any]
) -> None:
    """Same precedence as every other package in this repository: an explicit argument, else
    `ALGENTA_BASE_URL`, else the localhost fallback -- observed through `/healthz`, which is the
    one place a replica reports its resolved configuration. Outside a Serve replica there is no
    replica tag, and the constructor already reports that honestly as `"unknown"` rather than
    failing (inside one, the conformance suite sees >=2 distinct real tags via `REPLICA_HEADER`)."""
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    assert await make_proxy().healthz() == {
        "status": "ok",
        "replica": "unknown",
        "upstream_base_url": DEFAULT_UPSTREAM_BASE_URL,
    }

    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://env.test:9000/mcp")
    assert (await make_proxy().healthz())["upstream_base_url"] == "http://env.test:9000/mcp"

    explicit = make_proxy(upstream_base_url="http://arg.test:9001/mcp")
    assert (await explicit.healthz())["upstream_base_url"] == "http://arg.test:9001/mcp"


async def test_relays_the_real_stub_engine_byte_for_byte_over_a_real_socket(
    make_proxy: Callable[..., Any],
) -> None:
    """The byte-transparency claim, checked against the suite's real stub engine: a JSON-RPC
    `tools/list` sent through `mcp()` must produce the identical status, body and content-type as
    the same request sent to the stub directly -- with the hop-by-hop headers the stub's own
    uvicorn DID emit (`transfer-encoding`, `connection`) stripped, every other header relayed, and
    `REPLICA_HEADER` added. A non-2xx upstream answer (the stateless stub refuses `GET`/`HEAD`)
    must be relayed as-is too, never replaced by a proxy-authored error."""
    async with StubServerFixture() as stub:
        proxy = make_proxy(upstream_base_url=stub.base_url)
        async with httpx.AsyncClient() as direct:
            expected_post = await direct.post(stub.base_url, content=_TOOLS_LIST, headers=dict(_MCP_REQUEST_HEADERS))
            expected_get = await direct.get(stub.base_url, headers={"accept": "text/event-stream"})
            expected_head = await direct.head(stub.base_url)

        posted = await _drain(
            await proxy.mcp(
                _build_request(
                    "POST",
                    headers=(("host", "ray-proxy.internal"), ("content-length", str(len(_TOOLS_LIST))), *_MCP_REQUEST_HEADERS),
                    body=_TOOLS_LIST,
                )
            )
        )
        assert posted.status == expected_post.status_code == 200
        assert posted.body == expected_post.content
        assert b'"name":"query_data"' in posted.body
        assert posted.headers[REPLICA_HEADER] == "unknown"
        # Load-bearing for the stripping assertion: the upstream really did send these.
        assert "transfer-encoding" in expected_post.headers
        assert "connection" in expected_post.headers
        assert not _NEVER_RELAYED & set(posted.headers)
        for name, value in expected_post.headers.items():
            if name.lower() not in _NEVER_RELAYED and name.lower() != "date":
                assert posted.headers[name.lower()] == value, name

        fetched = await _drain(
            await proxy.mcp(_build_request("GET", headers=(("host", "ray-proxy.internal"), ("accept", "text/event-stream"))))
        )
        assert fetched.status == expected_get.status_code
        assert fetched.status != 200
        assert fetched.body == expected_get.content
        assert fetched.headers["allow"] == expected_get.headers["allow"]
        assert "content-length" in expected_get.headers
        assert "content-length" not in fetched.headers

        probed = await _drain(await proxy.mcp(_build_request("HEAD", headers=(("host", "ray-proxy.internal"),))))
        assert probed.status == expected_head.status_code
        assert probed.body == b""


async def test_request_side_contract_hop_by_hop_headers_dropped_everything_else_verbatim(
    make_proxy: Callable[..., Any],
) -> None:
    """What actually reaches the engine: method, the full query string, and the body byte for byte;
    end-to-end headers (`authorization`, MCP's own, any `x-*`) untouched; this hop's `host`,
    `content-length` and connection-management headers replaced by values httpx computes for the
    new hop (a client asking this proxy to close ITS connection must not close the engine's). The
    upstream's own `content-length` is not relayed downstream either."""
    async with _RecordingUpstream(
        status=202, headers=[("content-type", "application/json"), ("content-length", "2")], chunks=[b"{}"]
    ) as upstream:
        proxy = make_proxy(upstream_base_url=upstream.base_url)

        sent = await _drain(
            await proxy.mcp(
                _build_request(
                    "POST",
                    query=b"trace=abc&n=2",
                    headers=(
                        ("host", "ray-proxy.internal"),
                        ("content-length", "999"),
                        ("connection", "close"),
                        ("keep-alive", "timeout=5"),
                        ("te", "trailers"),
                        ("upgrade", "h2c"),
                        ("proxy-authorization", "Basic abc"),
                        ("authorization", "Bearer engine-key"),
                        ("mcp-protocol-version", "2025-06-18"),
                        ("x-request-id", "req-1"),
                        *_MCP_REQUEST_HEADERS,
                    ),
                    body=_TOOLS_LIST,
                )
            )
        )

        (forwarded,) = upstream.requests
        assert forwarded.method == "POST"
        assert forwarded.query == b"trace=abc&n=2"
        assert forwarded.body == _TOOLS_LIST
        assert forwarded.headers["host"] == f"127.0.0.1:{upstream.port}"
        assert forwarded.headers["content-length"] == str(len(_TOOLS_LIST))
        assert forwarded.headers["connection"] != "close"  # httpx's own value for the new hop
        for dropped in ("keep-alive", "te", "upgrade", "proxy-authorization"):
            assert dropped not in forwarded.headers, dropped
        for name, value in (
            ("authorization", "Bearer engine-key"),
            ("mcp-protocol-version", "2025-06-18"),
            ("x-request-id", "req-1"),
            *_MCP_REQUEST_HEADERS,
        ):
            assert forwarded.headers[name] == value, name

        assert sent.status == 202
        assert sent.body == b"{}"
        assert sent.headers["content-type"] == "application/json"
        assert sent.headers[REPLICA_HEADER] == "unknown"
        assert not _NEVER_RELAYED & set(sent.headers)


async def test_response_side_contract_streams_each_chunk_as_it_arrives_and_strips_hop_by_hop(
    make_proxy: Callable[..., Any],
) -> None:
    """Streamed, not buffered -- what `GET /mcp`'s event stream relies on. The upstream holds its
    second chunk until the first one has already been sent downstream, so a proxy that buffered
    the whole body before responding could never complete this request (the `wait_for` turns that
    deadlock into a failure). The upstream's connection-management headers are dropped; every
    other upstream header passes through."""
    first_forwarded = asyncio.Event()
    first, second = b"event: message\r\n", b'data: {"jsonrpc":"2.0","id":1,"result":{}}\r\n\r\n'
    async with _RecordingUpstream(
        headers=[
            ("content-type", "text/event-stream"),
            ("cache-control", "no-cache"),
            ("x-upstream-note", "kept"),
            ("connection", "keep-alive"),
        ],
        chunks=[first, (first_forwarded, second)],
    ) as upstream:
        proxy = make_proxy(upstream_base_url=upstream.base_url)
        response = await proxy.mcp(
            _build_request("GET", headers=(("host", "ray-proxy.internal"), ("accept", "text/event-stream")))
        )
        sent = await asyncio.wait_for(
            _drain(response, on_chunk=lambda _chunk: first_forwarded.set()), timeout=5.0
        )

        (forwarded,) = upstream.requests
        assert forwarded.method == "GET"
        assert forwarded.body == b""
        assert "content-length" not in forwarded.headers

        assert sent.status == 200
        assert len(sent.chunks) >= 2
        assert sent.body == first + second
        assert sent.headers[REPLICA_HEADER] == "unknown"
        assert sent.headers["content-type"] == "text/event-stream"
        assert sent.headers["cache-control"] == "no-cache"
        assert sent.headers["x-upstream-note"] == "kept"
        assert not _NEVER_RELAYED & set(sent.headers)


def _gzip_stream(pieces: list[bytes]) -> list[bytes]:
    """One gzip member spread over `len(pieces)` wire chunks: each piece is compressed and
    sync-flushed on its own, so an upstream can emit (and a test gate) every chunk independently
    while the concatenation stays a single stream that any gzip decoder, streaming or whole,
    accepts -- the shape a compressing server gives a `text/event-stream` response."""
    compressor = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    chunks = [compressor.compress(piece) + compressor.flush(zlib.Z_SYNC_FLUSH) for piece in pieces]
    chunks[-1] += compressor.flush()
    return chunks


async def test_compressed_upstream_reply_is_relayed_undecoded_under_its_own_content_encoding(
    make_proxy: Callable[..., Any],
) -> None:
    """A caller that offered `accept-encoding: gzip` and an engine that took the offer up. The proxy
    never decodes (`aiter_raw` yields wire bytes), so the bytes it relays are the compressed ones
    and the upstream's `content-encoding` label has to travel with them -- stripped, as it was
    until 0.1.4, a caller received gzip bytes with no way to recognise them. Asserted the way a
    real client sees it: an `httpx.Response` built from exactly the relayed status, headers and
    body decodes to the fixture's JSON. The caller's own offer, not httpx's default, is what
    reaches the engine."""
    payload = {"jsonrpc": "2.0", "id": 7, "result": {"tools": [{"name": "query_data"}]}}
    encoded = json.dumps(payload).encode()
    compressed = gzip.compress(encoded)
    async with _RecordingUpstream(
        headers=[
            ("content-type", "application/json"),
            ("content-encoding", "gzip"),
            ("content-length", str(len(compressed))),
        ],
        chunks=[compressed],
    ) as upstream:
        proxy = make_proxy(upstream_base_url=upstream.base_url)
        sent = await _drain(
            await proxy.mcp(
                _build_request(
                    "POST",
                    headers=(("host", "ray-proxy.internal"), ("accept-encoding", "gzip"), *_MCP_REQUEST_HEADERS),
                    body=_TOOLS_LIST,
                )
            )
        )

        (forwarded,) = upstream.requests
        assert forwarded.headers["accept-encoding"] == "gzip"

        assert sent.status == 200
        assert sent.headers["content-encoding"] == "gzip"
        assert sent.headers["content-type"] == "application/json"
        assert sent.headers[REPLICA_HEADER] == "unknown"
        assert not _NEVER_RELAYED & set(sent.headers)
        assert sent.body == compressed
        assert gzip.decompress(sent.body) == encoded
        as_a_client_sees_it = httpx.Response(sent.status, headers=sent.headers, content=sent.body)
        assert as_a_client_sees_it.json() == payload


async def test_compressed_event_stream_is_still_relayed_chunk_by_chunk_under_its_label(
    make_proxy: Callable[..., Any],
) -> None:
    """The same contract where it matters most, `text/event-stream`: a streaming caller decodes
    each compressed chunk as it arrives, so the proxy must both carry `content-encoding` on the
    response start (before any body byte) and keep forwarding chunk by chunk -- the second event
    is released by the upstream only after the first has been sent downstream, exactly as in the
    uncompressed streaming test, so a proxy that buffered to decode could never finish. The
    relayed chunks, fed to a streaming gzip decoder in arrival order, reproduce the events."""
    events = [
        b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\r\n\r\n',
        b'event: message\r\ndata: {"jsonrpc":"2.0","id":2,"result":{}}\r\n\r\n',
    ]
    chunks = _gzip_stream(events)
    first_forwarded = asyncio.Event()
    async with _RecordingUpstream(
        headers=[
            ("content-type", "text/event-stream"),
            ("content-encoding", "gzip"),
            ("cache-control", "no-cache"),
        ],
        chunks=[chunks[0], *((first_forwarded, chunk) for chunk in chunks[1:])],
    ) as upstream:
        proxy = make_proxy(upstream_base_url=upstream.base_url)
        response = await proxy.mcp(
            _build_request(
                "GET",
                headers=(
                    ("host", "ray-proxy.internal"),
                    ("accept", "text/event-stream"),
                    ("accept-encoding", "gzip, br"),
                ),
            )
        )
        sent = await asyncio.wait_for(
            _drain(response, on_chunk=lambda _chunk: first_forwarded.set()), timeout=5.0
        )

        (forwarded,) = upstream.requests
        assert forwarded.headers["accept-encoding"] == "gzip, br"

        assert sent.status == 200
        assert sent.headers["content-encoding"] == "gzip"
        assert sent.headers["content-type"] == "text/event-stream"
        assert sent.headers["cache-control"] == "no-cache"
        assert not _NEVER_RELAYED & set(sent.headers)
        assert len(sent.chunks) >= len(chunks)
        assert sent.body == b"".join(chunks)
        decoder = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
        streamed = b"".join(decoder.decompress(chunk) for chunk in sent.chunks) + decoder.flush()
        assert streamed == b"".join(events)


async def test_a_caller_that_offers_no_accept_encoding_is_not_offered_compression_on_its_behalf(
    make_proxy: Callable[..., Any],
) -> None:
    """httpx's own client default is `gzip, deflate, zstd`. A proxy that let it through would be
    negotiating compression for a caller that never asked for any and then -- correctly, now --
    relaying compressed bytes that caller may not expect. So when the caller sends no
    `accept-encoding` the upstream hop must see `identity`, and an upstream that then sends no
    `content-encoding` gets none invented for it downstream."""
    request_headers = (("host", "ray-proxy.internal"), *_MCP_REQUEST_HEADERS)
    assert "accept-encoding" not in {name for name, _value in request_headers}
    async with _RecordingUpstream(headers=[("content-type", "application/json")], chunks=[b"{}"]) as upstream:
        proxy = make_proxy(upstream_base_url=upstream.base_url)
        sent = await _drain(await proxy.mcp(_build_request("POST", headers=request_headers, body=_TOOLS_LIST)))

        (forwarded,) = upstream.requests
        assert forwarded.headers["accept-encoding"] == "identity"

        assert sent.status == 200
        assert sent.body == b"{}"
        assert "content-encoding" not in sent.headers


async def test_request_timeout_seconds_is_honoured_against_a_stalled_upstream(
    make_proxy: Callable[..., Any],
) -> None:
    """`request_timeout_seconds` reaches the replica's real HTTP client: an engine that stalls
    past it fails this request with httpx's timeout instead of hanging the replica."""
    async with _RecordingUpstream(delay_seconds=5.0, chunks=[b"{}"]) as upstream:
        proxy = make_proxy(upstream_base_url=upstream.base_url, request_timeout_seconds=0.2)
        with pytest.raises(httpx.TimeoutException):
            await proxy.mcp(_build_request("POST", headers=_MCP_REQUEST_HEADERS, body=_TOOLS_LIST))
        assert len(upstream.requests) == 1


def test_build_app_refuses_fewer_than_two_replicas_and_binds_otherwise() -> None:
    """`num_replicas` below 2 contradicts the one property this package exists to demonstrate and
    is refused with an explanation before anything is bound; 2 or more, or no override at all
    (the shipped default), yields a bound Serve application -- all without a running cluster."""
    for too_few in (0, 1):
        with pytest.raises(ValueError, match="at least 2"):
            build_app(num_replicas=too_few)
    assert isinstance(build_app(num_replicas=2), serve.Application)
    assert isinstance(build_app(), serve.Application)


def test_package_root_re_exports_exactly_the_deployment_modules_public_names() -> None:
    assert sorted(ray_serve_algenta.__all__) == sorted(deployment.__all__)
    for name in deployment.__all__:
        assert getattr(ray_serve_algenta, name) is getattr(deployment, name), name
