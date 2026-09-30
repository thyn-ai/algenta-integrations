"""Local demo: run a real `AlgentaToolset` against the repo's own stub MCP server.

No self-hosted engine and no model API key are needed: the stub server is a real
`fastmcp.FastMCP` HTTP server. This script calls one governed tool directly so the demo is
fully deterministic and requires no LLM provider setup.

This file is not part of the published `smolagents-algenta` package; it only works from a
checkout of the `algenta-integrations` repository.
"""

from __future__ import annotations

from smolagents_algenta import AlgentaToolset

# This repo's own test stub; not shipped on PyPI.
from tests.stub_server import StubServerFixture


def main() -> None:
    with StubServerFixture() as server:
        with AlgentaToolset(base_url=server.base_url, profile="observe") as toolset:
            recommend = next(tool for tool in toolset.tools if tool.name == "recommend")
            result = recommend.forward(scenario="expand-warehouse")
            print(result)


if __name__ == "__main__":
    main()
