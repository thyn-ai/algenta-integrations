"""End-to-end conformance tests: a real `litellm --config ...` proxy process, in front of a real
stub Algenta MCP server, driven over real HTTP -- never a mocked gateway and never a mocked
upstream. This is the real subject under test for a LiteLLM MCP Gateway integration: the config
this package generates, exercised against LiteLLM's own real, current (1.98.0) enforcement.

Every claim asserted below was independently, empirically verified during this package's design
research against a real running proxy (see `litellm_algenta.config`'s module docstring for the
source citations) -- these tests repeat those exact checks as an executable, CI-enforced suite
instead of asking a reader to trust that docstring.

Heavier than this repo's other packages' test suites by necessity: there is no in-process wrapper
object to exercise here, so "real" means spawning a real external process (see
`tests/proxy_fixture.py`). `test_profile_enforcement_and_receipts` intentionally bundles many
assertions behind one proxy startup (rather than paying that ~5-10s cost per assertion) --
sub-scenarios are laid out as clearly labeled blocks so a failure still points at exactly which
claim broke.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from litellm_algenta.config import build_mcp_server_entry
from litellm_algenta.contract import EXECUTE_DECISION, EXECUTE_DECISION_MODEL_FACING_PARAMS

from .proxy_fixture import LiteLLMProxyFixture
from .stub_server import PENDING_PLAN_HASH, REJECTED_PLAN_CODE, REJECTED_PLAN_HASH, StubServerFixture

_UPSTREAM_TOKEN = "algenta-upstream-bearer-token-for-tests"
_UPSTREAM_TOKEN_ENV_VAR = "ALGENTA_TEST_MCP_TOKEN"
_BASE_URL_ENV_VAR = "ALGENTA_TEST_MCP_URL"

pytestmark = pytest.mark.asyncio


def _merge_fragments(*fragments: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {"mcp_servers": {}}
    for fragment in fragments:
        merged["mcp_servers"].update(fragment["mcp_servers"])
    return merged


async def _list_tools(client: httpx.AsyncClient, proxy: LiteLLMProxyFixture) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{proxy.base_url}/mcp-rest/tools/list",
        headers={"Authorization": f"Bearer {proxy.master_key}"},
    )
    resp.raise_for_status()
    return resp.json()["tools"]


def _tool_names_by_server(tools: list[dict[str, Any]]) -> dict[str, set[str]]:
    by_server: dict[str, set[str]] = {}
    for tool in tools:
        server_name = tool["mcp_info"]["server_name"]
        by_server.setdefault(server_name, set()).add(tool["name"])
    return by_server


def _server_ids_by_name(tools: list[dict[str, Any]]) -> dict[str, str]:
    ids: dict[str, str] = {}
    for tool in tools:
        info = tool["mcp_info"]
        ids[info["server_name"]] = info["server_id"]
    return ids


async def _call_tool(
    client: httpx.AsyncClient,
    proxy: LiteLLMProxyFixture,
    *,
    server_id: str,
    name: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        f"{proxy.base_url}/mcp-rest/tools/call",
        headers={"Authorization": f"Bearer {proxy.master_key}", **(headers or {})},
        json={"name": name, "arguments": arguments, "server_id": server_id},
    )


async def test_profile_enforcement_and_receipts(tmp_path: Path) -> None:
    async with StubServerFixture(watched_token=_UPSTREAM_TOKEN, revoke_after=1000) as stub:
        config = _merge_fragments(
            build_mcp_server_entry(
                profile="observe",
                server_name="algenta_observe",
                base_url_env_var=_BASE_URL_ENV_VAR,
                authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
            ),
            build_mcp_server_entry(
                profile="govern",
                server_name="algenta_govern",
                base_url_env_var=_BASE_URL_ENV_VAR,
                authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
            ),
            build_mcp_server_entry(
                profile="execute",
                server_name="algenta_execute",
                base_url_env_var=_BASE_URL_ENV_VAR,
                authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
            ),
            build_mcp_server_entry(
                profile="full",
                server_name="algenta_full",
                base_url_env_var=_BASE_URL_ENV_VAR,
                authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
            ),
            # A "full"-profile server with no extra_headers, to prove header forwarding is
            # opt-in per server, not a gateway-wide default -- see the trace-header block below.
            build_mcp_server_entry(
                profile="full",
                server_name="algenta_full_no_trace_header",
                base_url_env_var=_BASE_URL_ENV_VAR,
                authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
                extra_headers=(),
            ),
        )
        env = {_BASE_URL_ENV_VAR: stub.base_url, _UPSTREAM_TOKEN_ENV_VAR: _UPSTREAM_TOKEN}

        async with LiteLLMProxyFixture(config, config_dir=tmp_path, env=env) as proxy:
            async with httpx.AsyncClient(timeout=10.0) as client:
                tools = await _list_tools(client, proxy)
                by_server = _tool_names_by_server(tools)
                server_ids = _server_ids_by_name(tools)

                # --- 1. allowed_tools narrows discovery to exactly each profile's tool set ----
                assert by_server["algenta_observe"] == {"get_contract", "query_data", "simulate", "recommend"}
                assert by_server["algenta_govern"] == by_server["algenta_observe"] | {
                    "plan_decision",
                    "log_decision",
                }
                assert by_server["algenta_execute"] == by_server["algenta_govern"] | {"execute_decision"}
                # "full" has no allowed_tools configured -- every tool the stub advertises, incl.
                # the test-only ones no real profile ever lists.
                assert by_server["algenta_full"] >= by_server["algenta_execute"] | {
                    "_test_approve_plan",
                    "echo_trace",
                    "flaky_after",
                }

                # --- 2. a non-envelope result (get_contract) passes through unchanged ---------
                resp = await _call_tool(
                    client, proxy, server_id=server_ids["algenta_observe"], name="get_contract", arguments={}
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["isError"] is False
                assert body["structuredContent"] == {
                    "capabilities": ["query", "simulate", "recommend"],
                    "engine_version": "1.4.0",
                }

                # --- 3. allowed_tools is a real call-time gate, not just a listing filter -----
                # execute_decision isn't in algenta_observe's allowed_tools -- calling it anyway
                # (bypassing the filtered list) must be refused server-side, by the gateway.
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_observe"],
                    name="execute_decision",
                    arguments={"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1"},
                )
                assert resp.status_code == 403

                # --- 4. never-model-facing fields: allowed_params rejects `force` at call time,
                #        even though this profile *does* include execute_decision -----------------
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1", "force": True},
                )
                assert resp.status_code == 403
                assert "force" in resp.json()["detail"]["error"]

                # ... and the same rejection holds under the "full" profile too -- the
                # never-model-facing rule is unconditional on profile, not just "the default-safe
                # ones" (see contract.py / config.py docstrings for why).
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_full"],
                    name="execute_decision",
                    arguments={"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1", "override_safety": True},
                )
                assert resp.status_code == 403

                # A call with only the contract-sanctioned params succeeds (still pending, since
                # nothing has approved this plan yet) -- proves the allowlist isn't overly broad.
                assert EXECUTE_DECISION_MODEL_FACING_PARAMS == {"plan_hash", "idempotency_key", "execution_id"}

                # --- 5. governed-execution receipt passthrough is byte-for-byte, unmodified ----
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1"},
                )
                assert resp.status_code == 200
                receipt = resp.json()["structuredContent"]
                assert resp.json()["isError"] is False  # gateway has zero opinion on approval_state
                assert receipt["approval_state"] == "pending"
                assert receipt["plan_hash"] == PENDING_PLAN_HASH
                assert receipt["execution_id"] == f"exec-{PENDING_PLAN_HASH}"
                assert receipt["status"] == "ok"
                assert receipt["code"] == "ok"
                assert receipt["result"] is None

                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"plan_hash": REJECTED_PLAN_HASH, "idempotency_key": "idem-1"},
                )
                assert resp.status_code == 200
                rejected_receipt = resp.json()["structuredContent"]
                assert resp.json()["isError"] is False  # still False -- a policy denial, not a tool error
                assert rejected_receipt["approval_state"] == "rejected"
                assert rejected_receipt["code"] == REJECTED_PLAN_CODE
                assert rejected_receipt["status"] == "error"

                # --- 6. the full approval lifecycle, end to end through the gateway ------------
                # _test_approve_plan isn't in any real profile's allowed_tools -- only reachable
                # via the "full" server, exactly as intended (admin/ops tooling, never model-facing).
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_full"],
                    name="_test_approve_plan",
                    arguments={"plan_hash": PENDING_PLAN_HASH},
                )
                assert resp.status_code == 200
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"plan_hash": PENDING_PLAN_HASH, "idempotency_key": "idem-1"},
                )
                assert resp.status_code == 200
                approved_receipt = resp.json()["structuredContent"]
                assert approved_receipt["approval_state"] == "approved"
                assert approved_receipt["result"] == {"executed": True, "plan_hash": PENDING_PLAN_HASH}

                # --- 7. extra_headers: caller->upstream header forwarding is real, opt-in, and
                #        one-directional (no round-trip back to the caller) -----------------------
                trace_value = "caller-trace-abc123"
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_full"],
                    name="echo_trace",
                    arguments={},
                    headers={"X-Trace-Id": trace_value},
                )
                assert resp.status_code == 200
                echoed = resp.json()["structuredContent"]["received_headers"]
                assert echoed["x-trace-id"] == trace_value
                # The gateway's own upstream credential was forwarded -- never the caller's
                # gateway-facing master key.
                assert echoed["authorization"] == f"Bearer {_UPSTREAM_TOKEN}"
                assert proxy.master_key not in (echoed["authorization"] or "")
                # And the gateway's own HTTP response back to the caller does NOT carry the
                # upstream's header back out -- any upstream signal has to live in the JSON body.
                assert "x-trace-id" not in {k.lower() for k in resp.headers}

                # Without extra_headers configured for a server, the header is NOT forwarded.
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_full_no_trace_header"],
                    name="echo_trace",
                    arguments={},
                    headers={"X-Trace-Id": trace_value},
                )
                assert resp.status_code == 200
                echoed_no_header = resp.json()["structuredContent"]["received_headers"]
                assert echoed_no_header["x-trace-id"] is None


async def test_oauth2_without_flow_fails_fast_at_proxy_startup(tmp_path: Path) -> None:
    """The real gateway's own enforcement of the oauth2/oauth2_flow requirement -- independent of
    (and a backstop for) this package's own `build_mcp_server_entry` raising the same thing at
    config-*build* time. Deliberately hand-writes the bad fragment rather than going through
    `build_mcp_server_entry`, since that function refuses to construct this in the first place."""
    bad_config = {
        "mcp_servers": {
            "algenta_bad": {
                "url": f"os.environ/{_BASE_URL_ENV_VAR}",
                "transport": "http",
                "auth_type": "oauth2",
                "client_id": "fake",
                "client_secret": "fake",
                # oauth2_flow deliberately omitted.
            }
        }
    }
    with pytest.raises((RuntimeError, TimeoutError)) as exc_info:
        async with LiteLLMProxyFixture(bad_config, config_dir=tmp_path, env={_BASE_URL_ENV_VAR: "http://127.0.0.1:1"}):
            pass
    assert "oauth2_flow" in str(exc_info.value)


async def test_static_bearer_token_401_becomes_iserror_without_retry(tmp_path: Path) -> None:
    """A mid-session upstream 401, for a static-credential auth_type, is swallowed into an MCP
    `isError: true` result carrying the raw stringified exception -- the gateway's own outer HTTP
    status stays 200, and there is no silent retry/reauth that would make a later call succeed
    again on the same (revoked) credential."""
    revoke_after = 2
    async with StubServerFixture(watched_token=_UPSTREAM_TOKEN, revoke_after=revoke_after) as stub:
        config = build_mcp_server_entry(
            profile="full",
            server_name="algenta_flaky",
            base_url_env_var=_BASE_URL_ENV_VAR,
            authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
        )
        env = {_BASE_URL_ENV_VAR: stub.base_url, _UPSTREAM_TOKEN_ENV_VAR: _UPSTREAM_TOKEN}

        async with LiteLLMProxyFixture(config, config_dir=tmp_path, env=env) as proxy:
            async with httpx.AsyncClient(timeout=10.0) as client:
                tools = await _list_tools(client, proxy)
                server_id = _server_ids_by_name(tools)["algenta_flaky"]

                for expected_count in range(1, revoke_after + 1):
                    resp = await _call_tool(
                        client, proxy, server_id=server_id, name="flaky_after", arguments={"key": "session-a"}
                    )
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["isError"] is False
                    assert body["structuredContent"]["result"]["call_count"] == expected_count

                # From here on, every call gets isError: true -- HTTP 200 at the gateway level,
                # no retry-with-a-fresh-token recovery for a static credential.
                for _ in range(2):
                    resp = await _call_tool(
                        client, proxy, server_id=server_id, name="flaky_after", arguments={"key": "session-a"}
                    )
                    assert resp.status_code == 200
                    body = resp.json()
                    assert body["isError"] is True
                    assert "401" in str(body["content"])
