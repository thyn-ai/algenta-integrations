"""Each profile exposes exactly the tool-name set the contract assigns it -- no additions, no
omissions -- exercised over the real wire against `tests/stub_server.py`.
"""

from __future__ import annotations

import pytest

from llamaindex_algenta import TOOL_PROFILES, create_algenta_tools


@pytest.mark.asyncio
async def test_observe_profile_exposes_exactly_its_contract_tools(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    names = {t.metadata.name for t in tools}
    assert names == TOOL_PROFILES["observe"]


@pytest.mark.asyncio
async def test_govern_profile_exposes_exactly_its_contract_tools(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="govern")
    names = {t.metadata.name for t in tools}
    assert names == TOOL_PROFILES["govern"]


@pytest.mark.asyncio
async def test_execute_profile_exposes_exactly_its_contract_tools(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="execute")
    names = {t.metadata.name for t in tools}
    assert names == TOOL_PROFILES["execute"]


@pytest.mark.asyncio
async def test_observe_profile_agent_cannot_even_see_execute_decision(stub_server: str) -> None:
    tools = await create_algenta_tools(base_url=stub_server, profile="observe")
    assert "execute_decision" not in {t.metadata.name for t in tools}
    assert "plan_decision" not in {t.metadata.name for t in tools}


@pytest.mark.asyncio
async def test_full_profile_exposes_every_tool_the_server_advertises_including_test_only_ones(stub_server: str) -> None:
    # `full` is a wildcard sentinel -- it exposes whatever the connected server's list_tools()
    # actually returns, test-only administrative tools included (this package cannot know ahead
    # of time which names a given deployment's "full" registry contains).
    tools = await create_algenta_tools(base_url=stub_server, profile="full")
    names = {t.metadata.name for t in tools}
    assert TOOL_PROFILES["execute"] <= names
    assert "_test_approve_plan" in names


@pytest.mark.asyncio
async def test_unknown_profile_is_rejected(stub_server: str) -> None:
    with pytest.raises(ValueError, match="Unknown tool profile"):
        await create_algenta_tools(base_url=stub_server, profile="admin")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_client_and_base_url_are_mutually_exclusive(stub_server: str) -> None:
    from llama_index.tools.mcp import BasicMCPClient

    client = BasicMCPClient(stub_server)
    with pytest.raises(ValueError, match="not both"):
        await create_algenta_tools(base_url=stub_server, client=client)
