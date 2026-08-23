"""End-to-end scenarios: real `create_algenta_tools` -> real
`langchain_mcp_adapters.client.MultiServerMCPClient` -> real `mcp` client -> real HTTP socket ->
the stub Algenta server in `tests/stub_server.py`.

`tests/test_approval_mapping.py` covers the `"pending"` -> `langgraph.types.interrupt(...)` ->
resume path specifically (it needs a real compiled graph, not just a bare tool call). This file
covers everything else that only needs a real wire round trip: profile filtering against a real
server's tool list, non-envelope passthrough, outright denial, and the never-model-facing scrub
holding over the real wire (not just the in-memory `tools=` path already covered in
`tests/test_never_model_facing.py`).
"""

from __future__ import annotations

import pytest

from langchain_algenta import AlgentaToolDenied, create_algenta_tools
from langchain_algenta.receipts import parse_receipt

from .stub_server import NON_ENVELOPE_RESULT, REJECTED_PLAN_CODE, REJECTED_PLAN_HASH


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
    # dicts); the JSON payload -- and therefore the receipt -- is embedded in the first block's
    # text, exactly like a real chat model would see it. Unwrap it the same way to assert on it.
    payload = _first_text_json(raw_result)
    receipt = parse_receipt(payload)
    assert receipt is not None
    assert receipt.approval_state == "none"
    assert receipt.result["recommended_action"] == "hold"


async def test_non_envelope_result_passes_through_unchanged(stub_server: str) -> None:
    # get_contract's discovery payload isn't a governed-execution envelope at all.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    get_contract = next(t for t in tools if t.name == "get_contract")

    raw_result = await get_contract.ainvoke({})

    payload = _first_text_json(raw_result)
    assert payload == NON_ENVELOPE_RESULT
    assert parse_receipt(payload) is None


async def test_execution_denied_outright_by_a_named_policy_gate(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")

    with pytest.raises(AlgentaToolDenied) as exc_info:
        await execute_decision.ainvoke({"plan_hash": REJECTED_PLAN_HASH})

    assert REJECTED_PLAN_CODE in str(exc_info.value)
    assert exc_info.value.receipt is not None
    assert exc_info.value.receipt.plan_hash == REJECTED_PLAN_HASH


async def test_never_model_facing_fields_absent_from_the_real_servers_tool_schema(stub_server: str) -> None:
    # The stub server's execute_decision genuinely declares `force` on its schema (mirroring the
    # real contract) -- create_algenta_tools must still strip it before the model ever sees it.
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")
    schema = execute_decision.args_schema
    assert "force" not in schema.get("properties", {})
    assert "plan_hash" in schema.get("properties", {})


async def test_a_smuggled_force_argument_never_reaches_the_real_server(stub_server: str) -> None:
    # Pre-approve a plan via the server's test-only admin tool (bypassing this package
    # entirely), then call execute_decision with a smuggled force=True and confirm the server's
    # own receipt shows it never received force=True.
    admin_tools = await create_algenta_tools(base_url=stub_server, profile="full")
    approve = next(t for t in admin_tools if t.name == "_test_approve_plan")
    await approve.ainvoke({"plan_hash": "plan-already-approved"})

    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    execute_decision = next(t for t in tools if t.name == "execute_decision")

    raw_result = await execute_decision.ainvoke({"plan_hash": "plan-already-approved", "force": True})

    payload = _first_text_json(raw_result)
    receipt = parse_receipt(payload)
    assert receipt is not None
    assert receipt.approval_state == "approved"
    assert receipt.result["forced"] is False


async def test_full_profile_exposes_the_test_only_admin_tool(stub_server: str) -> None:
    # `_test_approve_plan` isn't part of the real contract at all -- only `full` (opt-in,
    # everything the server advertises) should ever see it.
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    assert "_test_approve_plan" not in {t.name for t in tools}

    full_tools = await create_algenta_tools(base_url=stub_server, profile="full")
    assert "_test_approve_plan" in {t.name for t in full_tools}


def _first_text_json(raw_result: object) -> object:
    """Unwrap the JSON payload from a real MCP tool call's LangChain content-block result."""
    import json

    if isinstance(raw_result, list):
        for block in raw_result:
            if isinstance(block, dict) and block.get("type") == "text":
                return json.loads(block["text"])
    raise AssertionError(f"expected a list of text content blocks, got {raw_result!r}")
