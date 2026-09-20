# ray-serve-algenta

[![PyPI](https://img.shields.io/pypi/v/ray-serve-algenta.svg)](https://pypi.org/project/ray-serve-algenta/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../README.md)

Deploy Algenta's `/mcp` surface behind [Ray Serve](https://docs.ray.io/en/latest/serve/index.html):
`ray_serve_algenta.deployment.AlgentaMCPProxy`, a byte-transparent HTTP reverse proxy run as a
[KubeRay](https://github.com/ray-project/kuberay) `RayService`, plus a real multi-replica
conformance test suite.

**This package is not shaped like its `pydantic-ai-algenta` / `langchain-algenta` /
`haystack-algenta` siblings, on purpose.** None of those wrap an MCP client and expose typed
tools to a model-calling framework -- this one wraps nothing tool-shaped at all. Ray Serve is a
general-purpose model/service-serving layer; the only thing worth building here is proof that
Algenta's own `/mcp` route can sit safely behind it with real horizontal scale-out. What this
package ships:

- **`ray_serve_algenta.deployment.AlgentaMCPProxy`** -- a `@serve.deployment`-decorated,
  `@serve.ingress`-fronted class that forwards every request at `/mcp` (method, headers, query
  string, body -- all of it, streamed in both directions) to an operator-configured upstream
  Algenta engine. It never parses JSON-RPC content, never names an MCP tool, and never makes a
  routing decision based on what a request contains -- see [Why a raw reverse proxy, not an
  MCP-aware client/server pair](#why-a-raw-reverse-proxy-not-an-mcp-aware-clientserver-pair)
  below.
- **[`manifests/rayservice.yaml`](./manifests/rayservice.yaml)** -- a ready-to-adapt KubeRay
  `RayService` manifest: `autoscaling_config` with a floor of 2 replicas and a ceiling of 8, an
  upstream URL sourced from a Kubernetes `Secret` (never a literal endpoint), and inline comments
  on every prerequisite this repository does not set up for you (an image, the KubeRay operator,
  the Secret itself).
- **A real multi-replica conformance test suite** (`tests/`) that starts an actual `ray.init()` +
  `serve.run(...)` of the real `AlgentaMCPProxy` deployment with 2 live replicas, in front of a
  real stub Algenta MCP server, and proves two things over real HTTP -- see [Testing this
  package](#testing-this-package).

## Prerequisites

- **A running self-hosted Algenta engine**, reachable over HTTP, with its `/mcp` endpoint URL at
  hand (for example `http://localhost:8000/mcp`). This package never talks to any Algenta-hosted
  cloud service -- see [Self-hosted-first](#self-hosted-first) below. If you don't have an engine
  running yet, skip to [Try it locally](#try-it-locally-no-live-engine-required): it stands up a
  real stub engine for you, with nothing external to configure.
- **Python 3.12 or newer.**
- **No Algenta API key or credential is required by this package itself.** `AlgentaMCPProxy`
  forwards whatever headers a caller sends it, unchanged, straight to your upstream URL -- if your
  engine requires authentication, that's configured between the caller and the engine (or its
  ingress), not here. See [`manifests/rayservice.yaml`](./manifests/rayservice.yaml)'s header
  comment for the Kubernetes/KubeRay equivalent.

## Install

```bash
pip install ray-serve-algenta
```

Unlike `litellm-algenta`, where the config-generation code works standalone without `litellm`
itself installed, `ray[serve]` / `fastapi` / `httpx` are real, non-optional runtime dependencies
here -- `ray_serve_algenta.deployment` cannot be imported, let alone deployed, without them. See
[`pyproject.toml`](./pyproject.toml)'s dependency comment for a real, independently-reproduced gap
in `ray[serve]`'s own published extra (`jinja2` is imported unconditionally by `ray.serve`'s
internals but not declared as one of `ray[serve]`'s own dependencies as of `ray==2.58.0`) that
this package pins around so you don't have to rediscover it yourself.

## Why Algenta's `/mcp` transport makes this worth building at all

Algenta's public MCP endpoint contract states plainly: **the `/mcp` route implements stateless
Streamable HTTP.** Verified against the engine's real served behavior during this package's
design, not assumed from a changelog or an older memory of the protocol. Stateless means no
server-side session is pinned to the connection that created it -- a caller's *n*-th request does
not need to land on the same process, replica, or even the same physical machine as its *n-1*-th.
That is precisely the property that makes horizontal scale-out safe without extra machinery: no
sticky-session load balancer configuration, no shared session store between replicas, nothing for
`AlgentaMCPProxy` to coordinate. Every replica this package runs is fungible by construction,
because the upstream it forwards to was already fungible.

(One correction worth being explicit about: an earlier internal note referenced a "2026-07-28"
MCP spec revision as already shipped engine-side. The real, currently-live protocol revision on
the engine's `/mcp` route is `2025-11-25` -- the statelessness claim above holds regardless of
which spec revision is in effect, so this package does not depend on the "2026-07-28" figure at
all; it just doesn't repeat it.)

## Self-hosted-first

`AlgentaMCPProxy` forwards to **your own self-hosted Algenta engine**, resolved per replica, in
order, from:

1. `upstream_base_url=` passed to `build_app(...)` (an escape hatch -- production deployments
   should use option 2 below),
2. the `ALGENTA_BASE_URL` environment variable (what `manifests/rayservice.yaml` sets, from a
   `Secret`, on every head and worker pod),
3. `http://localhost:8000/mcp` (the same self-hosted-first fallback every in-process package in
   this repository uses -- see `pydantic_ai_algenta.toolset.DEFAULT_ALGENTA_BASE_URL` -- kept for
   consistency with every other package's documented default, not because it's safe to leave
   unset here specifically: `serve run` binds Ray Serve's own HTTP proxy to `localhost:8000` by
   default too, so running this package locally with `ALGENTA_BASE_URL` unset makes it forward
   every request to itself instead of to an engine. Always set `ALGENTA_BASE_URL` explicitly to
   your engine's real address before running this package -- see [Quick start
   (local)](#quick-start-local) below. Meaningless inside a container regardless, where nothing is
   listening on its own loopback).

Never an Algenta-hosted default, at any layer.

## Try it locally (no live engine required)

Nothing above requires a real Algenta engine to see working -- this package's own test suite
already includes a real stub one (`tests/stub_server.py`, a genuine `fastmcp.FastMCP` server
running in `stateless_http=True` mode, the same setting the real engine's `/mcp` route uses).
[`examples/try_it_locally.py`](./examples/try_it_locally.py) starts that stub, deploys the real
`AlgentaMCPProxy` in front of it, and makes one real MCP tool call through the whole path --
nothing mocked, no external network, nothing to configure:

```bash
cd python
uv sync --package ray-serve-algenta --all-extras
uv run python ray-serve-algenta/examples/try_it_locally.py
```

Expect a few seconds of genuine Ray/Serve startup logging, followed by:

```
Stub Algenta engine listening at http://127.0.0.1:54798/mcp (stands in for your real one)
AlgentaMCPProxy is up at http://127.0.0.1:8000/mcp, forwarding to the stub above

Real MCP response, round-tripped through the real proxy:
{'dataset': 'try-it-locally', 'request_id': 'demo-1', 'rows': [{'value': 1}, {'value': 2}]}
```

(The stub's own port is assigned by your OS and will differ each run -- only the final response
matters.)

That response traveled through the real `AlgentaMCPProxy` code over real HTTP, exactly like it
would against your own engine -- only the endpoint it forwarded to is a stub. Once you have a real
self-hosted Algenta engine running, move to [Quick start (local)](#quick-start-local) below and
point `ALGENTA_BASE_URL` at it instead.

## Quick start (local)

```bash
# Your own self-hosted Algenta engine's MCP endpoint -- must be a DIFFERENT host:port from the
# one this proxy binds below. `serve run` starts Ray Serve's HTTP proxy on its own default,
# localhost:8000; pointing ALGENTA_BASE_URL at that same address makes this proxy forward every
# request to itself instead of to your engine. Substitute wherever your engine actually listens.
export ALGENTA_BASE_URL="http://localhost:9000/mcp"

serve run ray_serve_algenta.deployment:app   # binds Ray Serve's default HTTP port, 8000
```

`ray_serve_algenta.deployment:app` is bound with the package's default `autoscaling_config`
(`min_replicas=2`, `max_replicas=8`, `target_ongoing_requests=10`) -- `serve run` will start 2
replicas immediately. Point any MCP client at `http://localhost:8000/mcp`; it talks to it exactly
like the real engine's own `/mcp` endpoint, because every byte this proxy sees is exactly what it
forwards.

## Quick start (Kubernetes / KubeRay)

```bash
kubectl create secret generic algenta-mcp-upstream \
  --from-literal=base-url="http://algenta-engine.your-namespace.svc.cluster.local:8000/mcp"
kubectl apply -f manifests/rayservice.yaml
```

See the manifest's own header comment for the prerequisites it does not set up for you (the
KubeRay operator, a container image with this package installed).

## Why a raw reverse proxy, not an MCP-aware client/server pair

Every other Python package in this repository sits *inside* an agent process, translating between
a model-calling framework's own tool-calling shape and Algenta's MCP tool surface --
`pydantic-ai-algenta` wraps `pydantic_ai.mcp.MCPToolset`, `haystack-algenta` wraps Haystack's own
`MCPToolset`, and so on. Ray Serve is not a model-calling framework and has no tool-calling shape
of its own to translate into -- it is infrastructure for *running* a service at scale. The only
honest thing to build here is what an infrastructure layer actually needs: a deployment that gets
the bytes from a caller to the upstream engine and back, correctly, under real concurrent load,
across real replicas. Teaching this proxy to parse JSON-RPC, recognize `execute_decision`, or
apply the tool-profile contract would not make it more capable -- it would just be a second,
parallel (and, given `scripts/check-no-engine-dependency.py`, forbidden) reimplementation of logic
the connected engine already owns, sitting in the one place in this repository's whole design that
was supposed to stay a dumb pipe. Tool-profile enforcement for traffic that happens to pass through
this proxy remains entirely the connected engine's job, exactly as it is for direct-to-engine MCP
traffic that never goes through Ray Serve at all.

## Why no `algenta-sdk` dependency

Same reasoning as `litellm-algenta` and `llamaindex-algenta`: every package
in this repository may depend on at most one Algenta-owned thing, the published `algenta-sdk`
client -- but only if something in the package would actually use it. `AlgentaMCPProxy` forwards
raw HTTP bytes with `httpx`; it never constructs an MCP client, never calls a tool, and has no use
for an SDK object of any kind. Declaring `algenta-sdk` anyway, unused, would repeat exactly the
leftover-placeholder-dependency pattern an adversarial review is on record catching elsewhere in
this repository's history (see `litellm-algenta`'s own README section of the same name).

## Testing this package

The conformance suite (`tests/test_multi_replica_conformance.py`) starts a real `ray.init()` local
cluster and a real `serve.run(...)` of the real `AlgentaMCPProxy` deployment with 2 live replicas,
in front of a real stub Algenta MCP server (`tests/stub_server.py`, a real `fastmcp.FastMCP`
server on a real HTTP socket, run in `stateless_http=True` mode to match the real engine's own
documented behavior) -- never a mocked deployment and never a mocked upstream. It proves, over
real HTTP with a real `fastmcp.Client`:

1. **No cross-replica state leakage** -- many real MCP tool calls, each carrying a unique
   `request_id`, fired concurrently and interleaved through the proxy; every response must echo
   back exactly its own call's arguments.
2. **Real multi-replica routing, not just multi-replica configuration** -- every response carries
   an `X-Algenta-Ray-Replica` header naming which replica served it (see `deployment.py`'s
   `REPLICA_HEADER`); the test asserts more than one distinct replica actually appears across a
   batch of calls, so a routing bug that always happened to answer from replica 0 would fail this
   check even though it would pass check 1.
3. **The documented `ALGENTA_BASE_URL` env-var resolution path**, exercised end to end rather than
   only through the `upstream_base_url=` escape hatch the other two tests use for convenience.

A fourth, fast, no-Ray-runtime-needed test (`test_default_autoscaling_config_has_a_floor_of_at_
least_two_replicas`) guards the shipped `autoscaling_config` default itself -- the other three
tests all pin `num_replicas=2` explicitly via `build_app(..., num_replicas=2)` for CI speed and
determinism (real autoscaler up/down-scaling behavior is slow and nondeterministic to assert on in
CI, and asserting on it would not add anything to what this package actually needs to prove), so
none of them exercise the real default on its own.

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest ray-serve-algenta -v
```

Each Ray-backed test takes real wall-clock seconds (`ray.init`/`serve.run`/`serve.shutdown`/
`ray.shutdown` each cost several seconds) -- assertions are bundled into as few full start/stop
cycles as the suite can honestly get away with, the same tradeoff `litellm-algenta`'s real-proxy
conformance suite documents for its own, structurally similar, external-process test cost.

No GPU is required anywhere in this package or its test suite -- `AlgentaMCPProxy` does no
computation of its own.
