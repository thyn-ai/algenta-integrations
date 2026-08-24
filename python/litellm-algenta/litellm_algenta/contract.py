"""The tool-profile contract, embedded for runtime use.

This module is a *runtime-embedded copy* of the profile/tool-name boundaries defined in
[`contracts/integration-tool-contract.json`](../../../contracts/integration-tool-contract.json)
at the root of the `algenta-integrations` repository. It has to be embedded rather than read
from that file at import time: once this package is installed from PyPI, the JSON contract
file living in its source monorepo is not on disk anymore, so the constants below are this
package's copy of record -- exactly the same pattern `pydantic_ai_algenta.contract` and
`langchain_algenta.contract` already use.

This is *not* a license to invent a different boundary. `tests/test_contract_parity.py` loads
the real contract file (when running inside a checkout of `algenta-integrations`, i.e. in this
repository's own CI) and asserts, byte-for-byte on the tool-name sets, that these constants
agree with it. If the contract file changes, that test fails here until this module is updated
to match -- so drift between the contract and this package is a test failure, not a silent
possibility.

Unlike the pydantic-ai and LangChain siblings, nothing in this package calls a tool directly --
there is no Python object that makes an MCP call. What consumes these constants instead is
`litellm_algenta.config`, which maps each profile onto the real, gateway-enforced
`allowed_tools` (and, for `execute`, `allowed_params`) fields of a LiteLLM `mcp_servers.<name>`
config entry. The enforcement boundary for this package lives in LiteLLM's proxy process at
request time, not in a Python method call -- see that module's docstring for exactly what's
verified to be real about that enforcement (source-cited and empirically confirmed against a
running `litellm --config ...` proxy, not assumed).
"""

from __future__ import annotations

from typing import Final, Literal

ToolProfile = Literal["observe", "govern", "execute", "full"]
"""The four tool profiles defined by the shared tool-profile contract.

See `contracts/integration-tool-contract.json` in the repository root for the authoritative
description of each. `"observe"` is the default for every integration package in this
repository unless a caller explicitly opts into a higher profile.
"""

DEFAULT_PROFILE: Final[ToolProfile] = "observe"

#: The full MCP tool-registry names referenced by the contract's per-profile tool lists (the
#: `"full"` profile additionally exposes everything else the connected engine's MCP tool
#: registry advertises -- this package cannot enumerate that ahead of time since it varies by
#: engine version and deployment, hence `"full"`'s `tools: "*"` in the contract).
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
#: Mirrors the contract's `profiles.full.tools == "*"`. There is no equivalent LiteLLM
#: `allowed_tools` sentinel for "everything" -- `"full"` is expressed by *omitting*
#: `allowed_tools`/`disallowed_tools` entirely from the gateway config entry, which is what
#: `build_mcp_server_entry` does for this profile (see `config.py`).
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
discovered by the gateway at connect time, not known ahead of time by this package.
"""

#: Fields that exist on `execute_decision`'s schema for operator/break-glass use only. No
#: profile may turn these into a model-facing parameter -- see the contract's
#: `profiles.execute.never_model_facing_note`. `build_mcp_server_entry` enforces this at the one
#: layer LiteLLM's gateway config actually gives it: `allowed_params.execute_decision`, which
#: LiteLLM enforces as a real, call-time HTTP 403 (`mcp_server_manager.py`'s
#: `validate_allowed_params`, source-cited and empirically verified in `config.py`'s docstring)
#: if a caller's arguments contain a key outside the allowed list. It does **not**, and cannot,
#: remove `force`/`override_safety` from the JSON Schema the gateway *advertises* for the tool
#: at discovery time -- see `config.py`'s module docstring for why, and for what an operator
#: still has to do upstream.
NEVER_MODEL_FACING_FIELDS: Final[frozenset[str]] = frozenset({"force", "override_safety"})

#: The `execute_decision` parameters this package's `execute`/`full` profile templates put in
#: `allowed_params.execute_decision` -- every real parameter on the tool's actual request schema
#: that the model is allowed to supply, and nothing else. The real tool takes `decision_id`
#: (required -- the id returned by an already-called `log_decision`) and `webhook_url` (required
#: -- where the engine delivers the execution), plus the optional `timeout_seconds` and `metadata`.
#: There is no `plan_hash`, `idempotency_key`, or `execution_id` argument anywhere on the real
#: tool: `execute_decision` is not called against a `plan_decision` plan_hash at all, and the
#: engine's own idempotency gate (see `EXECUTE_DECISION_GATES` below) is keyed off `decision_id`
#: server-side, not a caller-supplied key. Deliberately excludes every field in
#: `NEVER_MODEL_FACING_FIELDS`.
EXECUTE_DECISION_MODEL_FACING_PARAMS: Final[frozenset[str]] = frozenset(
    {"decision_id", "webhook_url", "timeout_seconds", "metadata"}
)

#: The three, real, literal gate names `execute_decision` can name in a synchronous `409` denial
#: (`{"error": {"code": "execution_blocked_<gate>", "gate": "<gate>", "message": ...,
#: "override_hint": ...}}`). There is no fourth gate and no async/pending state: a call either
#: returns a `200` `ExecutionReceipt` or one of these three denials, in the same call.
#:
#: - `"idempotency"` -- this decision was already delivered; bypassable only via `force=true`,
#:   for one re-execution.
#: - `"confidence"` -- the logged decision's confidence is below `policy.min_confidence`;
#:   bypassable only via `override_safety=true`.
#: - `"risk_floor"` -- the logged decision's `risk_p5` is below `-policy.risk_floor`; bypassable
#:   only via `override_safety=true`.
#:
#: `force` and `override_safety` are exactly `NEVER_MODEL_FACING_FIELDS` -- a model-facing caller
#: can never self-bypass any of these three gates, by construction, regardless of profile.
EXECUTE_DECISION_GATES: Final[frozenset[str]] = frozenset({"idempotency", "confidence", "risk_floor"})


def resolve_profile_tool_names(profile: ToolProfile, *, available_tool_names: frozenset[str]) -> frozenset[str]:
    """Resolve a profile to the concrete tool names it allows, given what the server advertises.

    For `"full"`, that's every name the connected engine's MCP registry advertises right now
    (`available_tool_names`, unfiltered). For the other three profiles, it's the contract's
    fixed set intersected with what's actually available -- a tool the contract names but the
    connected engine doesn't currently advertise is simply absent, not an error.

    Used by `tests/test_contract_parity.py` and by `config.py`'s builders; not consulted by any
    running gateway (the gateway does its own equivalent filtering against `allowed_tools`).
    """
    allowed = TOOL_PROFILES[profile]
    if allowed == FULL_PROFILE_SENTINEL:
        return available_tool_names
    return frozenset(allowed) & available_tool_names
