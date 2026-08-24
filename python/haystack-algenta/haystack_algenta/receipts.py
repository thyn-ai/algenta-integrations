"""`ExecutionReceipt` / `ExecutionBlocked` -- the typed shapes of a real `execute_decision` MCP
tool call's result, plus the plumbing that recovers either one from what a real Haystack
`MCPToolset` tool call actually returns.

**Corrected against the real engine contract.** An earlier version of this module modeled a single
generic `GovernedExecutionReceipt` envelope (`status`/`code`/`retryable`/`plan_hash`/
`approval_state`/`execution_id`/...) that every governed tool call was assumed to return, including
an async `"pending"` approval state resumable via a later out-of-band approval. That shape does not
exist anywhere in the real engine: grepping the real `apps/` tree (outside tests) for
`GovernedExecutionReceipt`/`receipt_version`/`approval_state` returns zero hits. The real
`execute_decision` MCP tool takes `decision_id` + `webhook_url` (no `plan_hash`, no caller-supplied
idempotency key) and either:

- succeeds (200) with a real `ExecutionReceipt`: `{decision_id, webhook_url, execution_status:
  "delivered"|"failed", response_code, executed_at, policy_snapshot_id, schema_snapshot_id,
  manifest_version, payload_summary, safety_overridden}`, or
- is blocked synchronously (409) by exactly one of three real, named policy gates --
  `"idempotency"` (bypassable only via `force=True`, for one re-execution), `"confidence"`, or
  `"risk_floor"` (both bypassable only via `override_safety=True`) -- with body `{"error": {"code":
  "execution_blocked_<gate>", "gate": "<gate>", "message": "...", "override_hint": "..."}}`.

There is no third, "come back later" outcome: a call is a same-response success or a same-response
denial, never a pause. `ExecutionReceipt.execution_status` (`"delivered"` vs `"failed"`) describes
whether the *webhook delivery itself* succeeded, not a policy decision -- a `"failed"` delivery is
still a completed, non-gated `execute_decision` call, not something this module treats as an error.

Only `execute_decision` is known to return either of these two shapes. Every other tool this
package's `create_algenta_tools` can expose (`get_contract`, `query_data`, `simulate`, `recommend`,
`plan_decision`, `log_decision`) has its own, unrelated result shape -- `parse_execution_outcome`
returns `None` for anything that doesn't validate as one of the two shapes above, which is the
deliberate signal callers use to pass such a result through unchanged.

`unwrap_mcp_tool_result` below is unaffected by any of this -- it recovers the real JSON payload
from a real `Tool.invoke()` call's double-JSON-string MCP envelope regardless of what that payload
turns out to mean; see the package README's "Why the double-JSON unwrap" section for how that was
verified live against installed `haystack-ai` 3.0.0 / `mcp-haystack` 1.4.1.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ExecutionGate = Literal["idempotency", "confidence", "risk_floor"]
"""The exactly-three real, named policy gates `execute_decision` can synchronously block a call on
(a real 409 with `error.code == f"execution_blocked_{gate}"`). No other gate name exists.

- `"idempotency"`: this `decision_id` was already delivered and `force` was not `True`.
  Bypassable only via `force=True`, described by the real schema as "Override the idempotency gate
  for one re-execution" -- i.e. a single-shot override, not a standing exemption.
- `"confidence"`: the decision's confidence is below `policy.min_confidence`. Bypassable only via
  `override_safety=True`.
- `"risk_floor"`: `risk_p5` is below `-policy.risk_floor`. Bypassable only via
  `override_safety=True`.
"""


class ExecutionReceipt(BaseModel):
    """The real success (200) result of an `execute_decision` MCP tool call.

    `extra="allow"` on purpose: the engine may add fields to this envelope over time, and a newer
    engine talking to an older version of this package should not fail to parse just because it
    sent one more field than this model knew about when it was released.
    """

    model_config = ConfigDict(extra="allow")

    decision_id: str
    webhook_url: str
    execution_status: Literal["delivered", "failed"]
    """Whether the webhook delivery itself succeeded -- **not** a policy verdict. A `"failed"`
    delivery is still a real, completed, non-gated `execute_decision` call; it is not something
    `haystack_algenta.hooks.GovernedReceiptHook` treats as a denial or raises for."""

    response_code: int | None = None
    """The webhook target's own HTTP response code, when `execution_status == "delivered"`."""

    executed_at: str | None = None
    policy_snapshot_id: str | None = None
    schema_snapshot_id: str | None = None
    manifest_version: str | None = None
    payload_summary: Any = None
    safety_overridden: bool = False
    """Whether `override_safety=True` was applied for this call (an operator/break-glass field --
    never model-facing; see `haystack_algenta.contract.NEVER_MODEL_FACING_FIELDS`)."""

    def is_delivered(self) -> bool:
        return self.execution_status == "delivered"


class ExecutionBlocked(BaseModel):
    """The real synchronous 409 denial body's `error` object -- `execute_decision` was blocked by
    exactly one of the three real, named gates (see `ExecutionGate`).

    `extra="allow"` for the same forward-compatibility reason as `ExecutionReceipt`.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    """The real error code, always literally `f"execution_blocked_{gate}"`."""

    gate: ExecutionGate
    message: str
    override_hint: str | None = None
    """A human-readable hint about which never-model-facing field bypasses this specific gate
    (e.g. "Override the idempotency gate for one re-execution.")."""


def parse_execution_outcome(
    payload: Any,
    *,
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    blocked_model: type[ExecutionBlocked] = ExecutionBlocked,
) -> ExecutionReceipt | ExecutionBlocked | None:
    """Parse an already-unwrapped `execute_decision` tool-result payload into either an
    `ExecutionReceipt` (success) or an `ExecutionBlocked` (one of the three real named-gate
    denials).

    Returns `None` when `payload` isn't a dict, or is a dict that validates as neither shape --
    e.g. `get_contract`'s discovery payload, `log_decision`'s own result shape, or any other
    non-`execute_decision` tool's result. This is the deliberate signal callers use to pass such a
    result through unchanged.

    A top-level `"error"` key that is itself a dict is always attempted as `ExecutionBlocked`
    first (matching the real 409 body's `{"error": {...}}` shape) and, if it doesn't validate that
    way, treated as unparseable rather than falling through to an `ExecutionReceipt` attempt that
    could never succeed anyway (a denial body never carries `decision_id`/`webhook_url` at its top
    level).
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        try:
            return blocked_model.model_validate(error)
        except ValidationError:
            return None
    try:
        return receipt_model.model_validate(payload)
    except ValidationError:
        return None


def unwrap_mcp_tool_result(raw: Any) -> Any:
    """Unwrap the double-JSON envelope a real `MCPToolset`-built `Tool`'s call actually returns.

    Verified live (see the package README): `Tool.invoke()` for an MCP-derived tool returns a
    JSON *string* of the whole MCP `CallToolResult` -- `{"meta": ..., "content": [{"type": "text",
    "text": "<the tool's real JSON payload, itself a string>"}], "structuredContent": ...,
    "isError": ...}`. This function peels both string layers to recover the real payload as a
    plain Python value (typically a `dict`).

    Also accepts (defensively, and to support the `tools=`/`mcp_toolset=` escape hatches, whose
    fake tools may return a plain dict directly with no MCP `CallToolResult` wrapping at all):

    - a plain `dict` (returned as-is), and
    - a `dict` already carrying Haystack's own parsed `content` list (i.e. `raw` is a dict, not a
      JSON string, but still has the `content: [{"type": "text", "text": ...}]` shape) -- the
      shape `MCPToolset._connect_and_load_tools`'s `outputs_to_state` branch produces internally.

    Returns `None` when nothing recognizable is found -- the deliberate signal
    `parse_execution_outcome` (once fed this function's output) uses to treat a result as a
    non-`execute_decision` passthrough (e.g. `get_contract`'s discovery payload wrapped the same
    way), or when `raw` isn't parseable at all (e.g. a plain non-JSON string, or a genuine
    execution error message).
    """
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None

    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        # A real `CallToolResult`'s own `structuredContent` (a top-level sibling of `content`, per
        # the MCP spec) takes priority when present -- it's already the real, structured payload
        # with no second JSON-string layer to peel.
        structured = payload.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in payload["content"]:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if item.get("type") == "text" and isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        return None

    if isinstance(payload, dict):
        return payload

    return None


def extract_execution_outcome_from_tool_result(
    raw: Any,
    *,
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    blocked_model: type[ExecutionBlocked] = ExecutionBlocked,
) -> ExecutionReceipt | ExecutionBlocked | None:
    """`unwrap_mcp_tool_result` followed by `parse_execution_outcome` -- the one call
    `haystack_algenta.hooks.GovernedReceiptHook` needs to go from a real `ToolCallResult.result`
    string straight to a typed `ExecutionReceipt`/`ExecutionBlocked` (or `None` for a non-governed
    passthrough result)."""
    return parse_execution_outcome(unwrap_mcp_tool_result(raw), receipt_model=receipt_model, blocked_model=blocked_model)


__all__ = [
    "ExecutionBlocked",
    "ExecutionGate",
    "ExecutionReceipt",
    "extract_execution_outcome_from_tool_result",
    "parse_execution_outcome",
    "unwrap_mcp_tool_result",
]
