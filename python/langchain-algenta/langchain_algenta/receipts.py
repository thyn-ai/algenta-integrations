"""`ExecutionReceipt` / `ExecutionDenial` -- the typed shapes of a real `execute_decision` MCP
tool call's two, and only two, possible outcomes.

The real engine's `execute_decision` tool (`decision_id`, `webhook_url`, plus the operator-only
`force` / `override_safety` -- see `langchain_algenta.contract.NEVER_MODEL_FACING_FIELDS`) either:

- succeeds (HTTP 200 underneath) and returns a real `ExecutionReceipt` --
  `{decision_id, webhook_url, execution_status, response_code, executed_at, policy_snapshot_id,
  schema_snapshot_id, manifest_version, payload_summary, safety_overridden}`. `execution_status`
  being `"failed"` (the webhook target itself rejected delivery) is still this success shape --
  the *call* completed; only the downstream delivery didn't land -- not a policy denial.
- or is blocked *synchronously, in the same call* (HTTP 409 underneath) on exactly one of three
  named policy gates -- `"idempotency"`, `"confidence"`, or `"risk_floor"` -- and reports
  `{"error": {"code": "execution_blocked_<gate>", "gate": "<gate>", "message": ..., "override_hint":
  ...}}` as an MCP tool execution error (`CallToolResult(isError=True)`), parsed here into an
  `ExecutionDenial`.

There is no third, "pending" outcome and no separate approval-lifecycle state on this tool at
all -- a real, separate plan_hash+nonce human-approval system does exist on the engine, but its
own source says explicitly that it is intentionally not exposed as an MCP/LLM tool, so no
MCP-based integration package (this one included) can ever observe it. Earlier versions of this
package modeled a `GovernedExecutionReceipt` with `approval_state`/`plan_hash`/`code`/`status`
fields and paused via `langgraph.types.interrupt(...)` on a fictional `"pending"` state -- that
shape and that pause never corresponded to anything the real engine returns; see
`langchain_algenta.governance` and this package's README for the corrected model.

This module is deliberately identical in shape to its sibling packages' equivalents (see the
repository's other framework integrations) once they receive the same correction -- the receipt
and denial envelopes are one shared contract, not something each framework package gets to
redefine.
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ExecutionGate = Literal["idempotency", "confidence", "risk_floor"]
"""The exactly three named policy gates `execute_decision` can block on, verified directly
against the real engine's tool contract.

- `"idempotency"`: this `decision_id` was already delivered and `force` was not `true`. `force`
  bypasses *only* this gate, for one re-execution.
- `"confidence"`: the logged decision's `confidence` is below `policy.min_confidence`. Bypassable
  only via `override_safety=true`.
- `"risk_floor"`: the logged decision's `risk_p5` is below `-policy.risk_floor`. Bypassable only
  via `override_safety=true`.
"""

#: The exact three real gate names, as a set -- useful for membership checks and for validating
#: that a parsed `ExecutionDenial.gate` is one of the documented values rather than some future,
#: as-yet-unnamed gate this package's `ExecutionGate` literal doesn't know about yet.
NAMED_EXECUTION_GATES: Final[frozenset[str]] = frozenset({"idempotency", "confidence", "risk_floor"})


class ExecutionReceipt(BaseModel):
    """The real, successful `execute_decision` result envelope (the engine's HTTP 200 shape).

    `extra="allow"` on purpose: the engine may add fields to this envelope over time, and a newer
    engine talking to an older version of this package should not fail to parse just because it
    sent one more field than this model knew about when it was released.
    """

    model_config = ConfigDict(extra="allow")

    decision_id: str
    webhook_url: str
    execution_status: Literal["delivered", "failed"]
    """Whether the webhook delivery itself landed. `"failed"` here is a delivery-level outcome
    (the target endpoint rejected or never received the call) -- the `execute_decision` call
    still completed and returned this real receipt; it is not a policy denial (see
    `ExecutionDenial` for that, a structurally different, `isError` outcome)."""

    response_code: int
    """The HTTP status code the webhook target itself returned (or a transport-level code for a
    delivery that never reached it), not this MCP call's own outcome."""

    executed_at: str
    policy_snapshot_id: str
    schema_snapshot_id: str
    manifest_version: str

    payload_summary: Any = None
    """A summary of what was actually delivered to `webhook_url`."""

    safety_overridden: bool = False
    """Whether this execution required `override_safety=true` to get past the `"confidence"` or
    `"risk_floor"` gate. Always `False` for a call that never needed to bypass anything."""

    def is_delivered(self) -> bool:
        """Whether the webhook delivery itself landed (`execution_status == "delivered"`)."""
        return self.execution_status == "delivered"


class ExecutionDenial(BaseModel):
    """The real synchronous denial body -- the inner `error` object of the engine's `409`
    response `{"error": {"code": ..., "gate": ..., "message": ..., "override_hint": ...}}`.

    `extra="allow"` for the same forward-compatibility reason as `ExecutionReceipt`.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    """The specific, named error code, e.g. `"execution_blocked_idempotency"`."""

    gate: str
    """Which of the three real named gates blocked this call -- `"idempotency"`, `"confidence"`,
    or `"risk_floor"`. Typed as `str`, not `ExecutionGate`, so an engine that ever adds a fourth
    gate this package doesn't know about yet still parses instead of failing validation; check
    membership in `NAMED_EXECUTION_GATES` if you need to distinguish the documented three from an
    unknown future one."""

    message: str
    override_hint: str | None = None


def parse_receipt(raw_result: Any, *, model: type[ExecutionReceipt] = ExecutionReceipt) -> ExecutionReceipt | None:
    """Parse a wrapped MCP tool's raw, non-error result payload into an `ExecutionReceipt`, or
    `None`.

    Returns `None` (rather than raising) when `raw_result` doesn't validate as a real execution
    receipt -- e.g. any other tool's own result shape (`get_contract`'s discovery payload,
    `log_decision`'s `{decision_id, chosen_action, ...}`, `plan_decision`'s plan summary -- none
    of these carry `webhook_url`/`execution_status`/`response_code`, so none of them validate).
    This is the deliberate signal callers use to tell "this was a real execute_decision success"
    apart from "this is some other tool's own, unrelated result".

    Args:
        raw_result: The wrapped MCP tool's raw, non-error result payload, already unwrapped from
            the MCP `CallToolResult` envelope (typically a dict -- see
            `langchain_algenta.interceptor.extract_call_tool_payload`).
        model: The `ExecutionReceipt` subclass to validate against.
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
    except ValidationError:
        return None


def _extract_embedded_json_object(text: str) -> Any:
    """Best-effort recovery of a JSON object embedded somewhere in `text`, tolerating
    surrounding prose.

    Some MCP server frameworks wrap a raised exception's own message in extra text before
    handing it back as an `isError=True` result's content -- e.g. `"Error executing tool
    execute_decision: {"error": {...}}"`, verified directly against the installed `mcp` SDK's
    `mcp.server.fastmcp.tools.base.Tool.run`, which does exactly this. Rather than requiring the
    denial body to be the *entire* error text (fragile, and not something this package's own
    server-side counterpart controls), this scans for the first `{` through the last `}` in
    `text` and tries to parse that span. Returns `None` if nothing in `text` parses as JSON.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_denial(raw_error_payload: Any, *, model: type[ExecutionDenial] = ExecutionDenial) -> ExecutionDenial | None:
    """Parse an MCP tool execution error's payload into an `ExecutionDenial`, or `None`.

    Accepts a `dict` (either the full `{"error": {...}}` envelope -- the real engine's `409`
    response body -- or the inner object directly), or a `str` (an error result's raw text
    content, or a raised exception's own message), from which a `dict` is recovered via
    `_extract_embedded_json_object` before validating. Exactly how the real MCP tool serializes
    a caught `409` into its error text -- and whether an intermediate framework layer wraps that
    text in its own prose -- is an implementation detail this package doesn't control, so both
    input shapes are handled and both denial-body shapes are tolerated, rather than requiring an
    exact match that a harmless serialization difference could break.

    Returns `None` for anything that doesn't validate as one of the three named gates (e.g. a
    generic transport failure, or a future error shape this package doesn't recognize yet) --
    callers treat that as "not a recognized policy denial", not as a certainty that nothing went
    wrong.
    """
    body: Any = raw_error_payload
    if isinstance(body, str):
        body = _extract_embedded_json_object(body)
    if not isinstance(body, dict):
        return None
    body = body.get("error", body)
    if not isinstance(body, dict):
        return None
    try:
        return model.model_validate(body)
    except ValidationError:
        return None


__all__ = [
    "NAMED_EXECUTION_GATES",
    "ExecutionDenial",
    "ExecutionGate",
    "ExecutionReceipt",
    "parse_denial",
    "parse_receipt",
]
