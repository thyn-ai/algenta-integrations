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

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from litellm_algenta.config import build_mcp_server_entry
from litellm_algenta.contract import EXECUTE_DECISION, EXECUTE_DECISION_GATES, EXECUTE_DECISION_MODEL_FACING_PARAMS

from .proxy_fixture import LiteLLMProxyFixture
from .stub_server import DECISION_ID_LOW_CONFIDENCE, DECISION_ID_RISK_FLOOR_BREACH, StubServerFixture

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


def _blocked_gate_error(call_response_json: dict[str, Any]) -> dict[str, Any]:
    """Extract the real `{"code": ..., "gate": ..., "message": ..., "override_hint": ...}` object
    from a blocked `execute_decision` call's `isError: true` result.

    The gate/code/message/override_hint travel inside the exception message the stub raised (see
    `stub_server.ExecutionBlockedError`), which FastMCP wraps as `"Error calling tool '<name>': "`
    plus the exception's own string in the first text content block of an `isError: true` result
    -- exactly what a real MCP-fronting `execute_decision` implementation would do with a real
    engine's `409` denial, since a JSON-RPC `tools/call` result carries no independent HTTP status
    of its own. This parses out FastMCP's own prefix to get back to the raw JSON payload.
    """
    assert call_response_json["isError"] is True
    text = call_response_json["content"][0]["text"]
    return json.loads(text[text.index("{") :])["error"]


def _blocked_gate(call_response_json: dict[str, Any]) -> str:
    return _blocked_gate_error(call_response_json)["gate"]


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
                assert by_server["algenta_full"] >= by_server["algenta_execute"] | {"echo_trace", "flaky_after"}

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
                    arguments={"decision_id": "decision-1", "webhook_url": "https://example.com/hook"},
                )
                assert resp.status_code == 403

                # --- 4. never-model-facing fields: allowed_params rejects `force` at call time,
                #        even though this profile *does* include execute_decision -----------------
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={
                        "decision_id": "decision-force-scrub",
                        "webhook_url": "https://example.com/hook",
                        "force": True,
                    },
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
                    arguments={
                        "decision_id": "decision-override-scrub",
                        "webhook_url": "https://example.com/hook",
                        "override_safety": True,
                    },
                )
                assert resp.status_code == 403

                # A call with only the contract-sanctioned params succeeds -- proves the
                # allowlist isn't overly broad, and that it's the real tool's real params.
                assert EXECUTE_DECISION_MODEL_FACING_PARAMS == {
                    "decision_id",
                    "webhook_url",
                    "timeout_seconds",
                    "metadata",
                }

                # --- 5. a normal execute_decision call round-trips the real ExecutionReceipt ----
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={
                        "decision_id": "decision-normal",
                        "webhook_url": "https://example.com/hook",
                        "timeout_seconds": 30,
                        "metadata": {"source": "conformance-test"},
                    },
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["isError"] is False  # a real 200 receipt, not a denial
                receipt = body["structuredContent"]
                assert receipt["decision_id"] == "decision-normal"
                assert receipt["webhook_url"] == "https://example.com/hook"
                assert receipt["execution_status"] == "delivered"
                assert receipt["response_code"] == 200
                assert receipt["safety_overridden"] is False
                assert "executed_at" in receipt
                assert "policy_snapshot_id" in receipt
                assert "schema_snapshot_id" in receipt
                assert "manifest_version" in receipt
                # No fictional fields anywhere on the real receipt.
                for fictional_field in ("plan_hash", "approval_state", "idempotency_key", "receipt_version"):
                    assert fictional_field not in receipt

                # --- 6. each of the three real named gates surfaces as a distinct isError:true
                #        denial, with the real gate name preserved -- never a pending/approval
                #        state, and never an HTTP-level 409 (MCP has no such thing) -------------
                assert EXECUTE_DECISION_GATES == {"idempotency", "confidence", "risk_floor"}

                # 6a. "confidence" -- a fixed, always-below-threshold decision_id.
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"decision_id": DECISION_ID_LOW_CONFIDENCE, "webhook_url": "https://example.com/hook"},
                )
                assert resp.status_code == 200  # the gateway's own outer HTTP status: still 200
                assert _blocked_gate(resp.json()) == "confidence"

                # 6b. "risk_floor" -- a fixed, always-below-floor decision_id.
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={
                        "decision_id": DECISION_ID_RISK_FLOOR_BREACH,
                        "webhook_url": "https://example.com/hook",
                    },
                )
                assert resp.status_code == 200
                assert _blocked_gate(resp.json()) == "risk_floor"

                # 6c. "idempotency" -- delivering the SAME decision_id a second time, organically
                #     (the stub's real semantics: a decision already delivered gates the retry).
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"decision_id": "decision-deliver-once", "webhook_url": "https://example.com/hook"},
                )
                assert resp.status_code == 200
                assert resp.json()["isError"] is False  # first delivery succeeds
                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_ids["algenta_execute"],
                    name="execute_decision",
                    arguments={"decision_id": "decision-deliver-once", "webhook_url": "https://example.com/hook"},
                )
                assert resp.status_code == 200
                assert _blocked_gate(resp.json()) == "idempotency"

                # There is no fourth state, and nothing left to "come back and check on" -- a
                # denial and a receipt are both final, in the same call that produced them.

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
    again on the same (revoked) credential. The same `isError: true` / outer-200 shape is what an
    `execute_decision` gate denial surfaces as -- see `test_profile_enforcement_and_receipts`'s
    gate block above."""
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
                    assert body["structuredContent"]["call_count"] == expected_count

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


async def test_execute_decision_gate_denial_surfaces_as_iserror(tmp_path: Path) -> None:
    """Focused, single-purpose companion to `test_profile_enforcement_and_receipts`'s gate block:
    proves there is no approval-pause code path left for `execute_decision` at all. A blocked call
    is a same-call, isError:true tool-call failure -- never a second state a caller comes back to
    poll. Kept as its own test (rather than folded further into the bundled one above) so a future
    regression toward a pending/approval shape fails with an unambiguous, single-claim test name.
    """
    async with StubServerFixture(watched_token=_UPSTREAM_TOKEN, revoke_after=1000) as stub:
        config = build_mcp_server_entry(
            profile="execute",
            server_name="algenta_execute",
            base_url_env_var=_BASE_URL_ENV_VAR,
            authentication_token_env_var=_UPSTREAM_TOKEN_ENV_VAR,
        )
        env = {_BASE_URL_ENV_VAR: stub.base_url, _UPSTREAM_TOKEN_ENV_VAR: _UPSTREAM_TOKEN}

        async with LiteLLMProxyFixture(config, config_dir=tmp_path, env=env) as proxy:
            async with httpx.AsyncClient(timeout=10.0) as client:
                tools = await _list_tools(client, proxy)
                server_id = _server_ids_by_name(tools)["algenta_execute"]

                resp = await _call_tool(
                    client,
                    proxy,
                    server_id=server_id,
                    name=EXECUTE_DECISION,
                    arguments={"decision_id": DECISION_ID_LOW_CONFIDENCE, "webhook_url": "https://example.com/hook"},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["isError"] is True
                error = _blocked_gate_error(body)
                assert error["code"] == "execution_blocked_confidence"
                assert error["gate"] == "confidence"
                assert "message" in error
                assert "override_hint" in error
                # No trace of the fictional async/pending shape anywhere in the denial.
                structured_content = body.get("structuredContent")
                assert not structured_content or "approval_state" not in structured_content
