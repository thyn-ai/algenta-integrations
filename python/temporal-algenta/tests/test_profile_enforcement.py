"""Profile-enforcement tests: the contract's four profiles gate which tools may be called, at
both layers this package enforces (the client's call-time check and the workflow-visible
advertised set used by the agent recipe).
"""

from __future__ import annotations

import pytest
from temporal_algenta.contract import (
    DEFAULT_PROFILE,
    ToolProfile,
    is_tool_allowed_for_profile,
    resolve_profile_tool_names,
)
from temporal_algenta.errors import AlgentaToolDenied

from .helpers import with_session_resilient

ALL_CONTRACT_TOOLS = frozenset(
    {"get_contract", "query_data", "simulate", "recommend", "plan_decision", "log_decision", "execute_decision"}
)


def test_default_profile_is_observe() -> None:
    from temporal_algenta.activities import AlgentaActivities

    assert DEFAULT_PROFILE == "observe"
    assert AlgentaActivities(base_url="http://unused").profile == "observe"


@pytest.mark.parametrize(
    ("profile", "allowed", "refused"),
    [
        ("observe", {"get_contract", "query_data", "simulate", "recommend"}, {"plan_decision", "log_decision", "execute_decision"}),
        ("govern", {"plan_decision", "log_decision", "simulate"}, {"execute_decision"}),
        ("execute", set(ALL_CONTRACT_TOOLS), set()),
        ("full", set(ALL_CONTRACT_TOOLS), set()),
    ],
)
def test_profile_membership_matrix(profile: ToolProfile, allowed: set[str], refused: set[str]) -> None:
    for name in allowed:
        assert is_tool_allowed_for_profile(name, profile), f"{name} should be allowed under {profile}"
    for name in refused:
        assert not is_tool_allowed_for_profile(name, profile), f"{name} should be refused under {profile}"


def test_resolve_profile_tool_names_intersects_with_available() -> None:
    # A tool the contract names but this engine doesn't advertise is simply absent.
    available = frozenset({"simulate", "recommend", "something_custom"})
    assert resolve_profile_tool_names("observe", available_tool_names=available) == frozenset({"simulate", "recommend"})
    assert resolve_profile_tool_names("full", available_tool_names=available) == available


async def test_client_refuses_out_of_profile_calls_across_profiles(stub_server) -> None:
    base_url, engine = stub_server
    for profile, refused_tool in [("observe", "log_decision"), ("govern", "execute_decision")]:
        with pytest.raises(AlgentaToolDenied):
            await with_session_resilient(
                base_url=base_url, profile=profile, fn=lambda c, t=refused_tool: c.call_tool(t, {})  # type: ignore[arg-type]
            )
    assert engine.execute_attempts == {}
    assert engine.logged_decisions == []


async def test_full_profile_reaches_test_diagnostics_but_execute_profile_does_not(stub_server) -> None:
    base_url, _engine = stub_server
    with pytest.raises(AlgentaToolDenied):
        await with_session_resilient(
            base_url=base_url, profile="execute", fn=lambda c: c.call_tool("_test_diagnostics")
        )
    result = await with_session_resilient(
        base_url=base_url, profile="full", fn=lambda c: c.call_tool("_test_diagnostics")
    )
    assert result == {"delivered_count": 0}
