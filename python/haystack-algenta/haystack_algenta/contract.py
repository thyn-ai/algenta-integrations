"""The tool-profile contract, embedded for runtime use.

This module is a *runtime-embedded copy* of the profile/tool-name boundaries defined in
[`contracts/integration-tool-contract.json`](../../../contracts/integration-tool-contract.json)
at the root of the `algenta-integrations` repository. It has to be embedded rather than read from
that file at import time: once this package is installed from PyPI, the JSON contract file living
in its source monorepo is not on disk anymore, so the constants below are this package's copy of
record.

This is *not* a license to invent a different boundary. `tests/test_contract_parity.py` loads the
real contract file (when running inside a checkout of `algenta-integrations`, i.e. in this
repository's own CI) and asserts, byte-for-byte on the tool-name sets, that these constants agree
with it. If the contract file changes, that test fails here until this module is updated to match
-- so drift between the contract and this package is a test failure, not a silent possibility.

This module is deliberately identical in shape (constants, names, docstrings) to its siblings in
`pydantic_ai_algenta.contract`, `langchain_algenta.contract`, `maf_algenta.contract`, and
`typescript/algenta-tools`'s `src/contract.ts` -- the tool-profile boundary is one shared decision,
not something each framework package gets to redefine.
"""

from __future__ import annotations

from typing import Final, Literal

ToolProfile = Literal["observe", "govern", "execute", "full"]
"""The four tool profiles defined by the shared tool-profile contract.

See `contracts/integration-tool-contract.json` in the repository root for the authoritative
description of each. `"observe"` is the default for every integration package in this repository
unless a caller explicitly opts into a higher profile.
"""

DEFAULT_PROFILE: Final[ToolProfile] = "observe"

#: The full MCP tool-registry names referenced by the contract's per-profile tool lists (the
#: `"full"` profile additionally exposes everything else the connected engine's MCP tool registry
#: advertises -- this package cannot enumerate that ahead of time since it varies by engine
#: version and deployment, hence `"full"`'s `tools: "*"` in the contract).
GET_CONTRACT: Final = "get_contract"
QUERY_DATA: Final = "query_data"
SIMULATE: Final = "simulate"
RECOMMEND: Final = "recommend"
PLAN_DECISION: Final = "plan_decision"
LOG_DECISION: Final = "log_decision"
EXECUTE_DECISION: Final = "execute_decision"

_OBSERVE_TOOLS: Final[frozenset[str]] = frozenset({GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND})
_GOVERN_TOOLS: Final[frozenset[str]] = _OBSERVE_TOOLS | {PLAN_DECISION, LOG_DECISION}
_EXECUTE_TOOLS: Final[frozenset[str]] = _GOVERN_TOOLS | {EXECUTE_DECISION}

#: `"full"` sentinel: every tool the connected engine's MCP registry advertises, opt-in only.
#: Mirrors the contract's `profiles.full.tools == "*"`.
FULL_PROFILE_SENTINEL: Final = "*"

TOOL_PROFILES: Final[dict[ToolProfile, frozenset[str] | Literal["*"]]] = {
    "observe": _OBSERVE_TOOLS,
    "govern": _GOVERN_TOOLS,
    "execute": _EXECUTE_TOOLS,
    "full": FULL_PROFILE_SENTINEL,
}
"""Profile name -> the exact set of MCP tool names exposed to the model.

`"full"` is the literal string `"*"` (matching the contract's own sentinel) rather than an
enumerated set, since the complete registry varies by connected engine/deployment and is
discovered at tool-listing time, not known ahead of time by this package.
"""

#: Every governed-execution tool named by the contract, excluding `get_contract` (a plain
#: discovery/capability listing, never an `execute_decision`-style outcome). Used only as a
#: documentation aid and a default scope for advanced callers of `haystack_algenta.hooks`; the
#: denial-mapping `after_tool` hook itself does not filter by tool name at all -- it detects the
#: real `execute_decision` denial shape by *shape*
#: (`haystack_algenta.receipts.parse_execution_outcome` returning an `ExecutionBlocked`), and any
#: other tool's result -- which never validates as `ExecutionReceipt`/`ExecutionBlocked` -- simply
#: passes through unchanged.
GOVERNED_TOOL_NAMES: Final[frozenset[str]] = _EXECUTE_TOOLS - {GET_CONTRACT}

#: Fields that exist on `execute_decision`'s schema for operator/break-glass use only. No profile,
#: and no tool-name filtering, may turn these into a model-facing parameter -- see the contract's
#: `profiles.execute.never_model_facing_note`. This package enforces that at two layers: it strips
#: these keys from a tool's advertised JSON schema (`Tool.parameters`) *and* from the arguments
#: dict actually forwarded to the wrapped MCP tool call, so a model that somehow still emitted one
#: of these as an argument (e.g. by copying it from a prior message) can't get it through either
#: layer.
NEVER_MODEL_FACING_FIELDS: Final[frozenset[str]] = frozenset({"force", "override_safety"})


def resolve_profile_tool_names(profile: ToolProfile, *, available_tool_names: frozenset[str]) -> frozenset[str]:
    """Resolve a profile to the concrete tool names it allows, given what the server advertises.

    For `"full"`, that's every name the connected engine's MCP registry advertises right now
    (`available_tool_names`, unfiltered). For the other three profiles, it's the contract's fixed
    set intersected with what's actually available -- a tool the contract names but the connected
    engine doesn't currently advertise is simply absent, not an error.
    """
    allowed = TOOL_PROFILES[profile]
    if allowed == FULL_PROFILE_SENTINEL:
        return available_tool_names
    return frozenset(allowed) & available_tool_names


def is_tool_allowed_for_profile(name: str, profile: ToolProfile) -> bool:
    """Whether `name` is allowed under `profile`, without reference to what a server advertises."""
    allowed = TOOL_PROFILES[profile]
    return allowed == FULL_PROFILE_SENTINEL or name in allowed


__all__ = [
    "DEFAULT_PROFILE",
    "EXECUTE_DECISION",
    "FULL_PROFILE_SENTINEL",
    "GET_CONTRACT",
    "GOVERNED_TOOL_NAMES",
    "LOG_DECISION",
    "NEVER_MODEL_FACING_FIELDS",
    "PLAN_DECISION",
    "QUERY_DATA",
    "RECOMMEND",
    "SIMULATE",
    "TOOL_PROFILES",
    "ToolProfile",
    "is_tool_allowed_for_profile",
    "resolve_profile_tool_names",
]
