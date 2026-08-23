"""`GovernedExecutionReceipt` -- the typed shape of a governed Algenta MCP tool call's result.

Every governed tool call against a self-hosted Algenta Engine (query/simulate/recommend and,
most importantly, `plan_decision` / `log_decision` / `execute_decision`) returns this envelope
as its result. `maf_algenta.toolset` parses the wrapped MCP function's raw result into this model
so the approval/denial/failure mapping (see that module) can be driven off typed fields
(`approval_state`, `code`, `status`) instead of raw dict indexing.

Not every tool a self-hosted Algenta MCP endpoint exposes necessarily returns this shape --
`get_contract`'s discovery payload, for instance, is a capability listing, not a governed
execution result. `parse_receipt` returns `None` for anything that doesn't validate as a
`GovernedExecutionReceipt`, and callers pass such results through unchanged as an ordinary
successful tool result.

This module is deliberately identical in shape to its siblings, `pydantic_ai_algenta.receipts`,
`langchain_algenta.receipts`, and `typescript/algenta-tools`'s `src/receipts.ts` -- the receipt
envelope is one shared contract, not something each framework package gets to redefine.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ApprovalState = Literal["none", "pending", "approved", "rejected", "expired"]
"""The engine's own approval-lifecycle state for a governed execution.

- `"none"`: no approval gate applies to this call (the common case for read-only tools).
- `"pending"`: the call is paused awaiting an out-of-band policy approval -- see
  `maf_algenta.toolset`, which raises `maf_algenta.AlgentaApprovalStillPending` for this state
  (a fail-closed `agent_framework.MiddlewareFailure`, since MAF has no resumable pause primitive
  at this layer -- see the package README for the full accounting).
- `"approved"`: the plan behind this call has been approved and the call executed.
- `"rejected"`: the plan was explicitly rejected by policy.
- `"expired"`: the approval window lapsed before the call could be resumed.
"""

#: `code` values that are named, already-shipped 409-style policy-gate denials on
#: `execute_decision` (see `contracts/integration-tool-contract.json`'s `execute` profile). A
#: receipt carrying one of these is a deliberate governance decision, not a transient failure --
#: `maf_algenta.toolset` raises `AlgentaToolDenied` for this, matching `approval_state ==
#: "rejected"`, rather than `AlgentaToolExecutionFailed`.
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
    """The caller-supplied idempotency key that also doubles as `execute_decision`'s
    single-use replay nonce (see the contract's `execute` profile `requires_all_of`)."""

    result: Any = None
    """The tool's actual payload (a recommendation, a query result, ...), once unwrapped from
    the governance envelope around it."""

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
    """Parse a wrapped MCP function's extracted result payload into a `GovernedExecutionReceipt`.

    Returns `None` (rather than raising) when `raw_result` doesn't validate as a governed
    execution envelope -- e.g. a dict missing `status`/`code`, or a non-dict value entirely.
    This is the deliberate signal callers use to pass a non-governed tool's result (such as
    `get_contract`'s discovery payload) through unchanged.

    Args:
        raw_result: The already-extracted JSON payload (typically a dict -- see
            `maf_algenta.toolset._extract_function_result_payload`).
        model: The `GovernedExecutionReceipt` subclass to validate against -- pass through a
            caller's typed subclass instead of always validating against the base model.
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
    except ValidationError:
        return None


__all__ = [
    "NAMED_POLICY_GATE_CODES",
    "ApprovalState",
    "GovernedExecutionReceipt",
    "parse_receipt",
]
