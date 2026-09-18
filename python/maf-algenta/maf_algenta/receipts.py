"""`ExecutionReceipt` / `ExecutionBlocked` -- the two, and only two, typed shapes a real
`execute_decision` MCP tool call can produce.

Verified directly against the real, running Algenta Engine's public MCP tool surface (observed
over the wire from this package) -- not assumed from this package's own prior README or any
planning document, both of which turned out to describe a fictional contract.
`execute_decision(decision_id,
webhook_url, timeout_seconds?, force?, override_safety?, metadata?)` either:

- succeeds (HTTP 200): a real `ExecutionReceipt` -- `decision_id`, `webhook_url`,
  `execution_status` (`"delivered"` or `"failed"` -- the *webhook delivery* outcome, not a
  governance verdict: even a `"failed"` delivery is a completed, successful call, and its receipt
  is real), `response_code`, `executed_at`, `policy_snapshot_id`, `schema_snapshot_id`,
  `manifest_version`, `payload_summary`, `safety_overridden`; or
- is blocked synchronously (HTTP 409), in the very same call, with a body shaped
  `{"error": {"code": "execution_blocked_<gate>", "gate": "idempotency" | "confidence" |
  "risk_floor", "message": ..., "override_hint": ...}}` -- `ExecutionBlocked` below.

There is no third state. No `plan_hash`, no `approval_state`, no async "pending" outcome, no
`receipt_version`/`execution_id`/`trace_id` field exists anywhere on this tool in the real engine
-- confirmed against the engine's public MCP endpoint contract for this tool, which carries none
of those fields. See `maf_algenta.exceptions` and
`maf_algenta.toolset` for how the 409 shape maps onto MAF's own fail-closed primitive.

This module is deliberately identical in shape to its (independently) corrected siblings,
`pydantic_ai_algenta.receipts`, `langchain_algenta.receipts`, and `typescript/algenta-tools`'s
`src/receipts.ts` -- the receipt envelope is one shared, real contract, not something each
framework package gets to redefine or re-fictionalize.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ExecutionGate = Literal["idempotency", "confidence", "risk_floor"]
"""The three, and only three, real named policy gates `execute_decision` can block on
synchronously (HTTP 409).

- `"idempotency"`: the decision was already delivered and this call didn't carry `force=true`.
  `force` bypasses this gate, and only this gate, for one re-execution.
- `"confidence"`: the decision's `confidence` is below `policy.min_confidence`. Bypassable only
  via `override_safety=true`.
- `"risk_floor"`: `risk_p5` is below `-policy.risk_floor`. Bypassable only via
  `override_safety=true`.
"""


class ExecutionReceipt(BaseModel):
    """The real, typed success result of a real `execute_decision` call (HTTP 200).

    `extra="allow"` on purpose: the engine may add fields to this envelope over time, and a newer
    engine talking to an older version of this package should not fail to parse just because it
    sent one more field than this model knew about when it was released.
    """

    model_config = ConfigDict(extra="allow")

    decision_id: str
    webhook_url: str
    execution_status: Literal["delivered", "failed"]
    """The outcome of *delivering* to `webhook_url` -- not a governance verdict. A `"failed"`
    delivery is still a successful, completed `execute_decision` call: a real receipt comes back,
    no exception is raised, and it's up to the caller to decide what to do about the failed
    delivery (e.g. inspect `response_code`, retry with a fresh call)."""
    response_code: int | None = None
    executed_at: str
    policy_snapshot_id: str | None = None
    schema_snapshot_id: str | None = None
    manifest_version: str | None = None
    payload_summary: Any = None
    safety_overridden: bool = False

    def is_delivered(self) -> bool:
        """Whether the webhook delivery itself succeeded (`execution_status == "delivered"`)."""
        return self.execution_status == "delivered"


class ExecutionBlocked(BaseModel):
    """The real, typed shape of the synchronous 409 `execute_decision` raises instead of ever
    returning a receipt, parsed out of the underlying MCP tool-error text by
    `maf_algenta.toolset` (see that module's `_parse_execution_blocked` for exactly how the
    `{"error": {...}}` body survives the real MCP wire round trip as an `isError` tool result).
    """

    model_config = ConfigDict(extra="allow")

    code: str
    """The real, literal `"execution_blocked_<gate>"` error code."""

    gate: str
    """One of the three real gate names (see `ExecutionGate`) -- kept as plain `str` here, not
    the `Literal`, so a gate name this package doesn't know about yet still parses as a real
    denial instead of silently failing to parse at all. `maf_algenta.toolset` raises
    `AlgentaToolDenied` for *any* gate value, known or not -- see that module."""

    message: str
    override_hint: str | None = None


def parse_execution_receipt(
    raw_result: Any, *, model: type[ExecutionReceipt] = ExecutionReceipt
) -> ExecutionReceipt | None:
    """Parse a real `execute_decision` HTTP-200 payload into an `ExecutionReceipt`.

    Returns `None` (rather than raising) when `raw_result` doesn't validate as that shape --
    `maf_algenta.toolset` treats that as a genuine anomaly for `execute_decision` specifically
    (see `AlgentaToolExecutionFailed`), since the real contract says a non-error `execute_decision`
    result is always a real receipt.

    Args:
        raw_result: The already-extracted JSON payload (typically a dict).
        model: The `ExecutionReceipt` subclass to validate against -- pass through a caller's
            typed subclass instead of always validating against the base model.
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
    except ValidationError:
        return None


def parse_execution_blocked(raw_error_body: Any) -> ExecutionBlocked | None:
    """Parse the real `{"error": {"code", "gate", "message", "override_hint"}}` 409 body.

    Returns `None` when `raw_error_body` doesn't validate as that shape -- the signal
    `maf_algenta.toolset` uses to tell a real, recognized policy-gate denial apart from some other,
    unrelated tool failure (which it re-raises unchanged rather than misrepresenting).
    """
    if not isinstance(raw_error_body, dict):
        return None
    error = raw_error_body.get("error")
    if not isinstance(error, dict):
        return None
    try:
        return ExecutionBlocked.model_validate(error)
    except ValidationError:
        return None


__all__ = [
    "ExecutionBlocked",
    "ExecutionGate",
    "ExecutionReceipt",
    "parse_execution_blocked",
    "parse_execution_receipt",
]
