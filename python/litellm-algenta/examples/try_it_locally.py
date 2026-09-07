"""Try litellm-algenta locally -- no self-hosted Algenta engine and no LLM provider key required.

This script generates a real `mcp_servers:` config, starts a real `litellm` proxy in front of a
real (but fake-data) stub Algenta MCP server, and drives both over real HTTP -- the same stub and
proxy fixtures this package's own test suite uses (`tests/stub_server.py`, `tests/proxy_fixture.py`),
run here as a standalone demo instead of inside `pytest`.

It proves the two things that matter before you point this at your own engine:

  1. `build_mcp_server_entry`'s profile really does control which tools the gateway exposes
     (`observe` sees four read-only tools; `execute_decision` isn't even reachable).
  2. A real `query_data` tool call round-trips real data through the real gateway.

It stops there -- there is no real Algenta engine or LLM provider key in this environment, so it
cannot run an actual chat completion. Wiring a real model to call these tools is the same as
always: point `ALGENTA_MCP_URL` at your own running engine and add an `mcp_servers` entry to your
own `model_list`-bearing `litellm` config (see the README's Quick start).

Run from this package's own directory, with the dev extras installed:

    cd python
    uv sync --all-packages --all-extras
    uv run python litellm-algenta/examples/try_it_locally.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

# Make this package's own `tests/` fixtures importable regardless of the caller's cwd -- they
# ship in the source checkout (not the built wheel), same as `configs/` in the Quick start's
# "skip the Python API" path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litellm_algenta.config import assert_safe, build_mcp_server_entry  # noqa: E402
from tests.proxy_fixture import LiteLLMProxyFixture  # noqa: E402
from tests.stub_server import StubServerFixture  # noqa: E402

_BASE_URL_ENV_VAR = "ALGENTA_MCP_URL"
_TOKEN_ENV_VAR = "ALGENTA_MCP_TOKEN"
_UPSTREAM_TOKEN = "local-demo-token"  # the stub's own fake credential -- not a real secret


def _print_header(title: str) -> None:
    print(f"\n== {title} ==")


async def main() -> None:
    print("litellm-algenta -- try it locally (no real Algenta engine, no LLM provider key)")

    async with StubServerFixture(watched_token=_UPSTREAM_TOKEN, revoke_after=1000) as stub:
        _print_header("1. Generate a real observe-profile config")
        fragment = build_mcp_server_entry(
            profile="observe",
            server_name="algenta",
            base_url_env_var=_BASE_URL_ENV_VAR,
            authentication_token_env_var=_TOKEN_ENV_VAR,
        )
        assert_safe(fragment)  # the same lint a hand-written config gets -- raises on any violation
        print(json.dumps(fragment, indent=2))
        print("assert_safe(fragment) passed -- no unsafe defaults, no Algenta-hosted URL.")

        with tempfile.TemporaryDirectory() as tmp:
            env = {_BASE_URL_ENV_VAR: stub.base_url, _TOKEN_ENV_VAR: _UPSTREAM_TOKEN}
            async with LiteLLMProxyFixture(fragment, config_dir=Path(tmp), env=env) as proxy:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    _print_header("2. A real litellm proxy is now running -- list what it exposes")
                    resp = await client.get(
                        f"{proxy.base_url}/mcp-rest/tools/list",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                    )
                    resp.raise_for_status()
                    tools = resp.json()["tools"]
                    tool_names = sorted(t["name"] for t in tools)
                    server_id = tools[0]["mcp_info"]["server_id"]
                    print(f"Tools visible under the 'observe' profile: {tool_names}")
                    assert "execute_decision" not in tool_names, "observe must never expose execute_decision"

                    _print_header("3. Call a real tool through the real gateway")
                    resp = await client.post(
                        f"{proxy.base_url}/mcp-rest/tools/call",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                        json={"name": "query_data", "arguments": {"dataset": "demo"}, "server_id": server_id},
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    print(f"query_data(dataset='demo') -> {json.dumps(body['structuredContent'])}")
                    assert body["isError"] is False

                    _print_header("4. Confirm the profile boundary is enforced server-side, not just hidden")
                    resp = await client.post(
                        f"{proxy.base_url}/mcp-rest/tools/call",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                        json={
                            "name": "execute_decision",
                            "arguments": {"decision_id": "demo", "webhook_url": "https://example.com/hook"},
                            "server_id": server_id,
                        },
                    )
                    print(
                        f"Calling execute_decision anyway (bypassing the filtered tool list) -> "
                        f"HTTP {resp.status_code} (refused by the gateway itself, not this package)"
                    )
                    assert resp.status_code == 403

    print("\nDone -- no real Algenta engine or LLM provider key was used above.")
    print("Point ALGENTA_MCP_URL at your own running engine to go further (see the README's Quick start).")


if __name__ == "__main__":
    asyncio.run(main())
