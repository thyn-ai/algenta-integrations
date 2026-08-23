"""`GovernedExecutionReceipt` -- the typed shape of a governed Algenta MCP tool call's result, plus
the plumbing that recovers it from what a real `mcp.types.CallToolResult` actually looks like.

Every governed tool call against a self-hosted Algenta Engine (query/simulate/recommend and, most
importantly, `plan_decision` / `log_decision` / `execute_decision`) returns this envelope as its
result. Not every tool a self-hosted Algenta MCP endpoint exposes necessarily returns this shape
-- `get_contract`'s discovery payload, for instance, is a capability listing, not a governed
execution result. `parse_receipt` returns `None` for anything that doesn't validate as a
`GovernedExecutionReceipt`, and `llamaindex_algenta.toolset`'s wrapped tool calls pass such
results through unchanged as an ordinary successful tool result.

This module is deliberately identical in the receipt model's shape to its siblings,
`pydantic_ai_algenta.receipts`, `langchain_algenta.receipts`, `maf_algenta.receipts`,
`haystack_algenta.receipts`, and `typescript/algenta-tools`'s `src/receipts.ts` -- the receipt
envelope is one shared contract, not something each framework package gets to redefine. What *is*
specific to this package is `unwrap_call_tool_result` / `extract_receipt_from_call_tool_result`
below: unlike Haystack's `MCPToolset` (whose `Tool.invoke()` returns the whole MCP
`CallToolResult` JSON-serialized to a *string*, with the tool's real payload nested as a second
JSON string inside a text content block), `llamaindex_algenta.toolset` calls
`BasicMCPClient.call_tool()` directly and gets back a real, typed `mcp.types.CallToolResult`
object (`content: list[ContentBlock]`, `structuredContent: dict | None`, `isError: bool`) --
verified live against a real `fastmcp` stub server: `structuredContent` is already the tool's
real, structured payload with no JSON-string layer to peel at all, and `content[0].text` carries
the same payload JSON-encoded as a fallback for servers that don't populate `structuredContent`.
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ApprovalState = Literal["none", "pending", "approved", "rejected", "expired"]
"""The engine's own approval-lifecycle state for a governed execution.

- `"none"`: no approval gate applies to this call (the common case for read-only tools).
- `"pending"`: the call is paused awaiting an out-of-band policy approval -- see
  `llamaindex_algenta.toolset`, which maps this onto the real
  `workflows.context.Context.wait_for_event()` human-in-the-loop primitive.
- `"approved"`: the plan behind this call has been approved and the call executed.
- `"rejected"`: the plan was explicitly rejected by policy.
- `"expired"`: the approval window lapsed before the call could be resumed.
"""

#: `code` values that are named, already-shipped 409-style policy-gate denials on
#: `execute_decision` (see `contracts/integration-tool-contract.json`'s `execute` profile). A
#: receipt carrying one of these is a deliberate governance decision, not a transient failure.
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
            `unwrap_call_tool_result` for recovering this from a real `CallToolResult`).
        model: The `GovernedExecutionReceipt` subclass to validate against -- pass through a
            caller's typed subclass instead of always validating against the base model.
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
    except ValidationError:
        return None


def is_call_error(raw: Any) -> bool:
    """Whether `raw` is a real `CallToolResult`/duck-typed equivalent reporting `isError=True`.

    This is the MCP *protocol*-level failure case (Q3, layer 1 in this package's research):
    something raised inside the connected server's own tool implementation, caught by the MCP
    SDK, and returned as ordinary (non-raising) response data with no governed-execution envelope
    to parse -- e.g. `content=[TextContent(text="Error executing tool blow_up: ...")]`. Distinct
    from a governed *denial* (`GovernedExecutionReceipt.is_denied()`), which is a deliberate,
    well-formed receipt the engine returned on purpose, not a protocol-level crash.
    """
    return bool(getattr(raw, "isError", False))


def call_error_text(raw: Any) -> str:
    """Best-effort human-readable text for an `is_call_error(raw)` result.

    Real MCP servers put the exception message in a `TextContent` block (e.g. `"Error executing
    tool blow_up: ..."`) -- this joins every such block, falling back to `str(raw)` if none are
    found (a duck-typed test double, for instance).
    """
    content = getattr(raw, "content", None) or []
    texts = [item.text for item in content if getattr(item, "type", None) == "text" and hasattr(item, "text")]
    return "; ".join(texts) if texts else str(raw)


def unwrap_call_tool_result(raw: Any) -> Any:
    """Unwrap a real `mcp.types.CallToolResult` (or a duck-typed/plain-dict equivalent, for tests
    and escape hatches that don't go through a real MCP wire) into the tool's actual payload.

    Preference order, verified live against a real `fastmcp` stub server round trip:

    1. `raw.structuredContent` when it's already a non-empty dict -- the real `CallToolResult`
       field the MCP spec reserves for exactly this, with no second JSON-string layer to peel.
    2. `raw.content`'s first `TextContent`-shaped block (`type == "text"`), `json.loads()`-ed --
       the fallback for a server that only populates `content`, not `structuredContent`.
    3. `raw` itself, if it's already a plain `dict` (the shape a fake/test `ClientSession` or a
       future non-MCP escape hatch might hand back directly, with no `CallToolResult` wrapping
       at all).
    4. `raw` itself, if it's a JSON string (defensive -- no real code path in this package
       produces this, since `BasicMCPClient.call_tool` always returns a typed object, but kept
       for symmetry with the sibling packages that do unwrap a JSON-string layer).

    Returns `None` when nothing recognizable is found -- the deliberate signal `parse_receipt`
    (once fed this function's output) uses to treat a result as a non-governed passthrough, or
    when `raw` genuinely isn't parseable (e.g. a non-JSON string).
    """
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    content = getattr(raw, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if getattr(item, "type", None) == "text" and isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        return None

    if isinstance(raw, dict):
        # Support a plain dict that already carries the MCP `content`/`structuredContent` shape
        # (e.g. a hand-built fake `CallToolResult`-like dict in a test), as well as a plain
        # already-unwrapped payload dict handed back by a non-MCP escape hatch.
        if isinstance(raw.get("structuredContent"), dict) and raw["structuredContent"]:
            return raw["structuredContent"]
        if isinstance(raw.get("content"), list):
            for item in raw["content"]:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if item.get("type") == "text" and isinstance(text, str):
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        continue
            return None
        return raw

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    return None


def extract_receipt_from_call_tool_result(
    raw: Any, *, model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt
) -> GovernedExecutionReceipt | None:
    """`unwrap_call_tool_result` followed by `parse_receipt` -- the one call
    `llamaindex_algenta.toolset`'s wrapped tool calls need to go from a real `CallToolResult`
    straight to a typed receipt (or `None` for a non-governed passthrough result)."""
    return parse_receipt(unwrap_call_tool_result(raw), model=model)


__all__ = [
    "NAMED_POLICY_GATE_CODES",
    "ApprovalState",
    "GovernedExecutionReceipt",
    "call_error_text",
    "extract_receipt_from_call_tool_result",
    "is_call_error",
    "parse_receipt",
    "unwrap_call_tool_result",
]
