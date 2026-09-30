"""Live walkthrough for the gateway profile-enforcement docs page.

This script performs the steps documented in
`docs/gateway-profile-enforcement.md` end-to-end with no self-hosted Algenta
engine and no LLM provider key. It starts a real stub Algenta MCP server, starts
a real `litellm` proxy with generated config, and demonstrates three enforcement
points:

  1. Under the `observe` profile, `execute_decision` is not in `allowed_tools`
     and the gateway returns a real HTTP 403.
  2. Under the `execute` profile, `execute_decision` is reachable but `force` is
     not in `allowed_params.execute_decision`, so the gateway returns a real
     HTTP 403.
  3. A valid `execute_decision` call returns the typed `ExecutionReceipt`
     synchronously, with `safety_overridden=False`.

Run from `algenta-integrations/python/`, with the dev extras installed:

    cd python
    uv sync --all-packages --all-extras
    python litellm-algenta/examples/profile_enforcement_walkthrough.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

# Make this package's own `tests/` fixtures importable regardless of the caller's
# cwd -- they ship in the source checkout (not the built wheel), same as
# `examples/try_it_locally.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litellm_algenta.config import assert_safe, build_mcp_server_entry  # noqa: E402
from tests.proxy_fixture import LiteLLMProxyFixture  # noqa: E402
from tests.stub_server import StubServerFixture  # noqa: E402

_BASE_URL_ENV_VAR = "ALGENTA_MCP_URL"
_TOKEN_ENV_VAR = "ALGENTA_MCP_TOKEN"
_UPSTREAM_TOKEN = "walkthrough-upstream-token"  # the stub's own fake credential


def _print_header(title: str) -> None:
    print(f"\n== {title} ==")


async def main() -> None:
    print(
        "litellm-algenta -- profile-enforcement walkthrough (no real engine, no LLM provider key)"
    )

    repo_root = Path(__file__).resolve().parents[3]
    contract_path = repo_root / "contracts" / "integration-tool-contract.json"
    contract = json.loads(contract_path.read_text())

    _print_header("1. Read the contract's observe profile")
    print(json.dumps(contract["profiles"]["observe"], indent=2))

    _print_header("2. Generate observe-profile YAML")
    observe_fragment = build_mcp_server_entry(
        profile="observe",
        server_name="algenta_observe",
        base_url_env_var=_BASE_URL_ENV_VAR,
        authentication_token_env_var=_TOKEN_ENV_VAR,
    )
    assert_safe(observe_fragment)
    observe_tools = observe_fragment["mcp_servers"]["algenta_observe"]["allowed_tools"]
    print(f"allowed_tools: {sorted(observe_tools)!r}")

    _print_header("3. Generate execute-profile YAML")
    execute_fragment = build_mcp_server_entry(
        profile="execute",
        server_name="algenta_execute",
        base_url_env_var=_BASE_URL_ENV_VAR,
        authentication_token_env_var=_TOKEN_ENV_VAR,
    )
    assert_safe(execute_fragment)
    execute_allowed_params = execute_fragment["mcp_servers"]["algenta_execute"]["allowed_params"][
        "execute_decision"
    ]
    print(f"allowed_params.execute_decision: {sorted(execute_allowed_params)!r}")

    async with StubServerFixture(watched_token=_UPSTREAM_TOKEN, revoke_after=1000) as stub:
        merged = {"mcp_servers": {}}
        merged["mcp_servers"].update(observe_fragment["mcp_servers"])
        merged["mcp_servers"].update(execute_fragment["mcp_servers"])
        env = {_BASE_URL_ENV_VAR: stub.base_url, _TOKEN_ENV_VAR: _UPSTREAM_TOKEN}

        with tempfile.TemporaryDirectory() as tmp:
            async with LiteLLMProxyFixture(merged, config_dir=Path(tmp), env=env) as proxy:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    _print_header("4. Start a real LiteLLM proxy with that config")
                    resp = await client.get(
                        f"{proxy.base_url}/mcp-rest/tools/list",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                    )
                    resp.raise_for_status()
                    tools = resp.json()["tools"]
                    by_server: dict[str, list[str]] = {}
                    server_ids: dict[str, str] = {}
                    for tool in tools:
                        info = tool["mcp_info"]
                        by_server.setdefault(info["server_name"], []).append(tool["name"])
                        server_ids[info["server_name"]] = info["server_id"]

                    observe_names = sorted(by_server["algenta_observe"])
                    print(f"Tools visible under the 'observe' profile: {observe_names}")
                    assert observe_names == sorted(observe_tools)

                    _print_header("5. Try execute_decision through the observe server")
                    resp = await client.post(
                        f"{proxy.base_url}/mcp-rest/tools/call",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                        json={
                            "name": "execute_decision",
                            "arguments": {
                                "decision_id": "walkthrough-observe-block",
                                "webhook_url": "https://example.com/hook",
                            },
                            "server_id": server_ids["algenta_observe"],
                        },
                    )
                    print(
                        "HTTP 403 -- refused by the gateway itself "
                        "(execute_decision is not in allowed_tools)"
                    )
                    assert resp.status_code == 403

                    _print_header("6. Promote to execute profile and try force=True")
                    resp = await client.post(
                        f"{proxy.base_url}/mcp-rest/tools/call",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                        json={
                            "name": "execute_decision",
                            "arguments": {
                                "decision_id": "walkthrough-force-block",
                                "webhook_url": "https://example.com/hook",
                                "force": True,
                            },
                            "server_id": server_ids["algenta_execute"],
                        },
                    )
                    print(
                        "HTTP 403 -- refused by the gateway itself "
                        "('force' is not in allowed_params.execute_decision)"
                    )
                    assert resp.status_code == 403
                    assert "force" in resp.json()["detail"]["error"]

                    _print_header("7. Make a valid execute_decision call")
                    resp = await client.post(
                        f"{proxy.base_url}/mcp-rest/tools/call",
                        headers={"Authorization": f"Bearer {proxy.master_key}"},
                        json={
                            "name": "execute_decision",
                            "arguments": {
                                "decision_id": "walkthrough-delivered",
                                "webhook_url": "https://example.com/hook",
                                "timeout_seconds": 30,
                                "metadata": {"source": "walkthrough"},
                            },
                            "server_id": server_ids["algenta_execute"],
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    assert body["isError"] is False
                    receipt = body["structuredContent"]
                    print(
                        f"ExecutionReceipt: decision_id={receipt['decision_id']}, "
                        f"execution_status={receipt['execution_status']}, "
                        f"safety_overridden={receipt['safety_overridden']}"
                    )
                    assert receipt["execution_status"] == "delivered"
                    assert receipt["safety_overridden"] is False
                    assert set(receipt.keys()) >= {
                        "decision_id",
                        "webhook_url",
                        "execution_status",
                        "response_code",
                        "executed_at",
                        "policy_snapshot_id",
                        "schema_snapshot_id",
                        "manifest_version",
                        "payload_summary",
                        "safety_overridden",
                    }

    print("\nDone.")
    print(
        "Point ALGENTA_MCP_URL at your own running engine to go further "
        "(see the README's Quick start)."
    )


if __name__ == "__main__":
    asyncio.run(main())
