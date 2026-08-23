"""Asserts `maf_algenta.contract`'s embedded constants agree with the real
`contracts/integration-tool-contract.json` file in this monorepo.

Skipped (not failed) when that file isn't found -- e.g. when this package's tests run against a
built wheel outside a checkout of `algenta-integrations`, where the contract file legitimately
doesn't exist on disk. Inside this repository's own CI, the file is always present, so this test
always runs there and is the real enforcement mechanism against contract/package drift. Mirrors
`python/pydantic-ai-algenta/tests/test_contract_parity.py` and
`python/langchain-algenta/tests/test_contract_parity.py` exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maf_algenta.contract import FULL_PROFILE_SENTINEL, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES


def _find_contract_file() -> Path | None:
    # tests/ -> maf-algenta/ -> python/ -> repo root
    candidate = Path(__file__).resolve().parents[3] / "contracts" / "integration-tool-contract.json"
    return candidate if candidate.is_file() else None


@pytest.fixture
def contract() -> dict:
    path = _find_contract_file()
    if path is None:
        pytest.skip("contracts/integration-tool-contract.json not found (not running inside a repo checkout)")
    return json.loads(path.read_text())


def test_observe_profile_matches_contract_exactly(contract: dict) -> None:
    assert TOOL_PROFILES["observe"] == frozenset(contract["profiles"]["observe"]["tools"])


def test_govern_profile_matches_contract_exactly(contract: dict) -> None:
    observe_tools = frozenset(contract["profiles"]["observe"]["tools"])
    govern_adds = frozenset(contract["profiles"]["govern"]["adds_tools"])
    assert contract["profiles"]["govern"]["extends"] == "observe"
    assert TOOL_PROFILES["govern"] == observe_tools | govern_adds


def test_execute_profile_matches_contract_exactly(contract: dict) -> None:
    govern_tools = TOOL_PROFILES["govern"]
    execute_adds = frozenset(contract["profiles"]["execute"]["adds_tools"])
    assert contract["profiles"]["execute"]["extends"] == "govern"
    assert TOOL_PROFILES["execute"] == govern_tools | execute_adds


def test_full_profile_is_the_wildcard_sentinel(contract: dict) -> None:
    assert contract["profiles"]["full"]["tools"] == "*"
    assert contract["profiles"]["full"].get("opt_in_only") is True
    assert TOOL_PROFILES["full"] == FULL_PROFILE_SENTINEL


def test_never_model_facing_fields_match_contract(contract: dict) -> None:
    assert NEVER_MODEL_FACING_FIELDS == frozenset(contract["profiles"]["execute"]["never_model_facing_fields"])


def test_every_mcp_tool_reference_name_is_used_by_some_profile(contract: dict) -> None:
    # Defends against a name being added to `mcp_tool_reference` without anyone deciding which
    # profile(s) it belongs in.
    referenced_names = set(contract["mcp_tool_reference"])
    profiled_names = set(TOOL_PROFILES["execute"])  # execute is the union of observe+govern+execute
    assert referenced_names == profiled_names
