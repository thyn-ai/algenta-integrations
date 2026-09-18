"""ray-serve-algenta: deploy Algenta's stateless `/mcp` surface behind Ray Serve.

The engine's public MCP endpoint contract states plainly that its canonical
`/mcp` route "implements stateless Streamable HTTP" -- verified against the live endpoint's
documented behavior, not assumed. This package exists to make that property *useful*: a
`ray_serve_algenta.deployment
.AlgentaMCPProxy` Ray Serve app that reverse-proxies every request to an operator-configured
upstream Algenta engine, run with >=2 replicas and no sticky-session requirement, because a
stateless upstream transport means no replica needs to remember, or share, anything about a
previous request.

This package never parses, generates, or reasons about MCP JSON-RPC content itself -- see
`ray_serve_algenta.deployment`'s module docstring for why that's deliberate. There is no tool-name
or tool-profile concept at this layer: profile enforcement remains entirely the connected engine's
job, exactly as it is for every other Algenta MCP endpoint this repository's packages talk to.
"""

from __future__ import annotations

from .deployment import (
    BASE_URL_ENV_VAR,
    DEFAULT_UPSTREAM_BASE_URL,
    MCP_PATH,
    REPLICA_HEADER,
    AlgentaMCPProxy,
    app,
    build_app,
)

__all__ = [
    "AlgentaMCPProxy",
    "BASE_URL_ENV_VAR",
    "DEFAULT_UPSTREAM_BASE_URL",
    "MCP_PATH",
    "REPLICA_HEADER",
    "app",
    "build_app",
]
