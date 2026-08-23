"""`GovernedExecutionReceipt` -- the typed shape of a governed Algenta MCP tool call's result, plus
the plumbing that recovers it from what a real Haystack `MCPToolset` tool call actually returns.

Every governed tool call against a self-hosted Algenta Engine (query/simulate/recommend and, most
importantly, `plan_decision` / `log_decision` / `execute_decision`) returns this envelope as its
result. Not every tool a self-hosted Algenta MCP endpoint exposes necessarily returns this shape
-- `get_contract`'s discovery payload, for instance, is a capability listing, not a governed
execution result. `parse_receipt` returns `None` for anything that doesn't validate as a
`GovernedExecutionReceipt`, and callers pass such results through unchanged as an ordinary
successful tool result.

This module is deliberately identical in the receipt model's shape to its siblings,
`pydantic_ai_algenta.receipts`, `langchain_algenta.receipts`, `maf_algenta.receipts`, and
`typescript/algenta-tools`'s `src/receipts.ts` -- the receipt envelope is one shared contract, not
something each framework package gets to redefine. What *is* specific to this package is
`unwrap_mcp_tool_result` / `extract_receipt_from_tool_result` below: verified live (see the
package README's "Why the double-JSON unwrap" section) that a real Haystack `Tool.invoke()` call
against an `MCPToolset`-built tool returns the raw MCP `CallToolResult`, JSON-serialized, with the
actual tool payload nested as a JSON *string* inside a text content block inside that JSON --
e.g. `'{"meta":null,"content":[{"type":"text","text":"{\\"status\\": \\"ok\\", ...}"}], ...}'`.
Neither `haystack.tools.tool.Tool` nor `haystack_integrations.tools.mcp.MCPToolset` ever unwraps
this -- there is no receipt concept anywhere in Haystack's own tool-calling layer -- so this
package has to.
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ApprovalState = Literal["none", "pending", "approved", "rejected", "expired"]
"""The engine's own approval-lifecycle state for a governed execution.

- `"none"`: no approval gate applies to this call (the common case for read-only tools).
- `"pending"`: the call is paused awaiting an out-of-band policy approval -- see
  `haystack_algenta.hooks.GovernedReceiptHook`, which raises
  `haystack_algenta.AlgentaApprovalStillPending` for this state (a fail-closed abort, since
  Haystack has no receipt-aware resumable pause primitive -- see the package README for the full
  accounting).
- `"approved"`: the plan behind this call has been approved and the call executed.
- `"rejected"`: the plan was explicitly rejected by policy.
- `"expired"`: the approval window lapsed before the call could be resumed.
"""

#: `code` values that are named, already-shipped 409-style policy-gate denials on
#: `execute_decision` (see `contracts/integration-tool-contract.json`'s `execute` profile). A
#: receipt carrying one of these is a deliberate governance decision, not a transient failure --
#: `haystack_algenta.hooks.GovernedReceiptHook` raises `AlgentaToolDenied` for this, matching
#: `approval_state == "rejected"`, rather than `AlgentaToolExecutionFailed`.
NAMED_POLICY_GATE_CODES: Final[frozenset[str]] = frozenset(
    {
        "plan_not_approved",
        "stale_plan",
        "plan_hash_mismatch",
        "idempotency_key_conflict",
    }
)

#: `status` values that indicate the call otherwise completed without an execution-level error.
#: Consulted only once `approval_state` and `code` have already been checked.
_SUCCESS_STATUSES: Final[frozenset[str]] = frozenset({"ok", "success"})


class GovernedExecutionReceipt(BaseModel):
    """The governed-execution result envelope every governed Algenta MCP tool call returns.

    `extra="allow"` on purpose: the engine may add fields to this envelope over time (it is
    versioned via `receipt_version`), and a newer engine talking to an older version of this
    package should not fail to parse just because it sent one more field than this model knew
    about when it was released.
    """

    model_config = ConfigDict(extra="allow")

    status: str
    """Coarse execution status as reported by the engine (e.g. `"ok"` or `"error"`)."""

    code: str
    """A specific, named result/error code (e.g. `"ok"`, `"plan_hash_mismatch"`, `"upstream_timeout"`)."""

    retryable: bool = False
    """Whether the engine considers a repeat of this exact call likely to succeed."""

    request_id: str | None = None
    trace_id: str | None = None
    policy_snapshot_hash: str | None = None
    receipt_version: int | str | None = None

    plan_hash: str | None = None
    """Hash of the decision plan this call is executing against, when one applies."""

    approval_state: ApprovalState = "none"

    execution_id: str | None = None
    """Identifies this specific governed-execution attempt, for later approval/audit lookups."""

    idempotency_key: str | None = None
    """The caller-supplied idempotency key that also doubles as `execute_decision`'s single-use
    replay nonce (see the contract's `execute` profile `requires_all_of`)."""

    result: Any = None
    """The tool's actual payload (a recommendation, a query result, ...), once unwrapped from the
    governance envelope around it."""

    def is_success(self) -> bool:
        """Whether this receipt represents a completed, non-gated, non-failed call."""
        return self.approval_state in ("none", "approved") and self.status in _SUCCESS_STATUSES

    def is_pending_approval(self) -> bool:
        return self.approval_state == "pending"

    def is_denied(self) -> bool:
        """Whether this receipt represents a deliberate governance denial (not a raw error)."""
        return self.approval_state in ("rejected", "expired") or self.code in NAMED_POLICY_GATE_CODES

    def denial_reason(self) -> str:
        """A human-readable reason for `is_denied()`, preferring the engine's own code/message."""
        message = self.result.get("message") if isinstance(self.result, dict) else None
        if message:
            return f"{self.code}: {message}"
        if self.approval_state == "rejected":
            return f"{self.code}: the decision plan was rejected by policy."
        if self.approval_state == "expired":
            return f"{self.code}: the approval window for this decision plan expired."
        return self.code


def parse_receipt(
    raw_result: Any, *, model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt
) -> GovernedExecutionReceipt | None:
    """Parse an already-unwrapped tool-result payload into a `GovernedExecutionReceipt`.

    Returns `None` (rather than raising) when `raw_result` doesn't validate as a governed
    execution envelope -- e.g. a dict missing `status`/`code`, or a non-dict value entirely. This
    is the deliberate signal callers use to pass a non-governed tool's result (such as
    `get_contract`'s discovery payload) through unchanged.

    Args:
        raw_result: The already-unwrapped JSON payload (typically a dict -- see
            `unwrap_mcp_tool_result` for recovering this from a real Haystack tool-call result).
        model: The `GovernedExecutionReceipt` subclass to validate against -- pass through a
            caller's typed subclass instead of always validating against the base model.
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
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

    Returns `None` when nothing recognizable is found -- the deliberate signal `parse_receipt`
    (once fed this function's output) uses to treat a result as a non-governed passthrough (e.g.
    `get_contract`'s discovery payload wrapped the same way), or when `raw` isn't parseable at
    all (e.g. a plain non-JSON string, or a genuine execution error message).
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


def extract_receipt_from_tool_result(
    raw: Any, *, model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt
) -> GovernedExecutionReceipt | None:
    """`unwrap_mcp_tool_result` followed by `parse_receipt` -- the one call
    `haystack_algenta.hooks.GovernedReceiptHook` needs to go from a real `ToolCallResult.result`
    string straight to a typed receipt (or `None` for a non-governed passthrough result)."""
    return parse_receipt(unwrap_mcp_tool_result(raw), model=model)


__all__ = [
    "NAMED_POLICY_GATE_CODES",
    "ApprovalState",
    "GovernedExecutionReceipt",
    "extract_receipt_from_tool_result",
    "parse_receipt",
    "unwrap_mcp_tool_result",
]
