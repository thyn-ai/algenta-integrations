"""End-to-end scenarios: real `create_algenta_tools` -> real
`langchain_mcp_adapters.client.MultiServerMCPClient` -> real `mcp` client -> real HTTP socket ->
the stub Algenta server in `tests/stub_server.py`.

`tests/test_execution_denial.py` covers the three real named policy gates on `execute_decision`
specifically. This file covers everything else that only needs a real wire round trip: profile
filtering against a real server's tool list, non-envelope passthrough for the other, freeform
tools, and the never-model-facing scrub holding over the real wire (not just the in-memory
`tools=` path already covered in `tests/test_never_model_facing.py`).
"""

from __future__ import annotations

import pytest

from langchain_algenta import AlgentaExecutionBlocked, create_algenta_tools
from langchain_algenta.receipts import parse_receipt

from .stub_server import NON_ENVELOPE_RESULT


async def test_observe_profile_matches_the_contract_against_a_real_server(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    assert {t.name for t in tools} == {"get_contract", "query_data", "simulate", "recommend"}


async def test_observe_profile_agent_cannot_even_call_execute_decision(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    assert "execute_decision" not in {t.name for t in tools}


async def test_successful_read_only_recommendation(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    recommend = next(t for t in tools if t.name == "recommend")

    raw_result = await recommend.ainvoke({"scenario": "expand-warehouse"})

    # The real MCP round trip returns LangChain content blocks (a list of {"type": "text", ...}
    # dicts); the JSON payload is embedded in the first block's text, exactly like a real chat
    # model would see it. `recommend` is a freeform, non-safety-critical tool -- its result is
    # never shaped like an ExecutionReceipt.
    payload = _first_text_json(raw_result)
    assert payload["recommended_action"] == "hold"
    assert parse_receipt(payload) is None


async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't an execution receipt at all.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    get_contract = next(t for t in tools if t.name == "get_contract")

    raw_result = await get_contract.ainvoke({})

    payload = _first_text_json(raw_result)
    assert payload == NON_ENVELOPE_RESULT
    assert parse_receipt(payload) is None


async def test_log_decision_result_is_not_mistaken_for_an_execution_receipt(stub_server: str) -> None:
    # log_decision returns {decision_id, chosen_action, expected_value, confidence, created_at,
    # note} -- it has a decision_id, but not webhook_url/execution_status/response_code, so it
    # must not validate as an ExecutionReceipt either.
    tools = await create_algenta_tools(base_url=stub_server, profile="govern")
    log_decision = next(t for t in tools if t.name == "log_decision")

    raw_result = await log_decision.ainvoke({"chosen_action": "hold"})

    payload = _first_text_json(raw_result)
    assert payload["decision_id"] == "decision-hold"
    assert parse_receipt(payload) is None


async def test_never_model_facing_fields_absent_from_the_real_servers_tool_schema(stub_server: str) -> None:
    # The stub server's execute_decision genuinely declares force/override_safety on its schema
    # (mirroring the real contract) -- create_algenta_tools must still strip both before the
    # model ever sees them.
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")
    schema = execute_decision.args_schema
    assert "force" not in schema.get("properties", {})
    assert "override_safety" not in schema.get("properties", {})
    assert "decision_id" in schema.get("properties", {})
    assert "webhook_url" in schema.get("properties", {})


async def test_a_smuggled_force_argument_never_reaches_the_real_server(stub_server: str) -> None:
    # Deliver a decision once (so the idempotency gate would otherwise fire on a repeat call),
    # then call again with a smuggled force=True and confirm it still gets blocked -- proving the
    # real server never actually received force=True, because the interceptor scrubbed it.
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")
    args = {"decision_id": "plan-already-delivered", "webhook_url": "https://example.com/hook"}

    first = await execute_decision.ainvoke(args)
    assert parse_receipt(_first_text_json(first)) is not None

    with pytest.raises(AlgentaExecutionBlocked) as exc_info:
        await execute_decision.ainvoke({**args, "force": True})

    assert exc_info.value.gate == "idempotency"


async def test_full_profile_exposes_the_test_only_admin_tool(stub_server: str) -> None:
    # `_test_diagnostics` isn't part of the real contract at all -- only `full` (opt-in,
    # everything the server advertises) should ever see it.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    assert "_test_diagnostics" not in {t.name for t in tools}

    full_tools = await create_algenta_tools(base_url=stub_server, profile="full")
    assert "_test_diagnostics" in {t.name for t in full_tools}


def _first_text_json(raw_result: object) -> object:
    """Unwrap the JSON payload from a real MCP tool call's LangChain content-block result."""
    import json

    if isinstance(raw_result, list):
        for block in raw_result:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block["text"])
    raise AssertionError(f"expected a list of text content blocks, got {raw_result!r}")
