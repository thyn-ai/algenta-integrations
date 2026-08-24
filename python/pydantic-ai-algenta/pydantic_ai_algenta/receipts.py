"""`ExecutionReceipt` / `ExecutionDenial` -- the two real, typed shapes `execute_decision` can
return.

`execute_decision` is the *only* Algenta MCP tool with a governed, typed result envelope.
Every other tool in the lifecycle -- `plan_decision` (a freeform `DecisionPlan` summary),
`log_decision` (`{decision_id, chosen_action, expected_value, confidence, created_at, note}`),
`record_outcome`, `get_decision`, `list_decisions`, `delete_decision` -- returns its own
freeform result, not a shared envelope. There is no generic "governed execution receipt" that
every tool call returns; that was this package's original, incorrect assumption.

`execute_decision(decision_id, webhook_url, timeout_seconds?, force?, override_safety?,
metadata?)` itself has exactly two real outcomes, both synchronous -- there is no "pending"
third state:

- Success (HTTP 200): an [`ExecutionReceipt`][pydantic_ai_algenta.receipts.ExecutionReceipt] --
  `{decision_id, webhook_url, execution_status: "delivered"|"failed", response_code,
  executed_at, policy_snapshot_id, schema_snapshot_id, manifest_version, payload_summary,
  safety_overridden}`. Note that `execution_status == "failed"` (the downstream webhook delivery
  itself failed) is still a *successful* `execute_decision` call -- the engine did what was
  asked and honestly reported the outcome. It is not a policy denial.
- A synchronous policy-gate denial (HTTP 409): an
  [`ExecutionDenial`][pydantic_ai_algenta.receipts.ExecutionDenial] --
  `{"error": {"code": "execution_blocked_<gate>", "gate": <gate>, "message", "override_hint"}}`,
  where `<gate>` is exactly one of the three real, named gates in `EXECUTION_GATES`.

`AlgentaToolset.call_tool` parses the wrapped tool's raw result against both shapes (denial
checked first, since an error body could coincidentally satisfy a loosely-typed success model)
and maps whichever one matches onto pydantic-ai's own primitives -- see `parse_receipt` /
`parse_denial` and the module docstring on `pydantic_ai_algenta.toolset`.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

ExecutionGate = Literal["idempotency", "confidence", "risk_floor"]
"""The three real, named policy gates `execute_decision` can synchronously block on.

- `"idempotency"`: this `decision_id` has already been delivered and `force` was not `true`.
  `force=true` bypasses *only* this gate, for one re-execution (per the real schema's own
  description: "Override the idempotency gate for one re-execution").
- `"confidence"`: the decision's confidence is below `policy.min_confidence`. Bypassable only
  via `override_safety=true`.
- `"risk_floor"`: `risk_p5` is below `-policy.risk_floor`. Bypassable only via
  `override_safety=true`.

There is no fourth gate and no asynchronous approval state -- a call either succeeds (200) or is
blocked by exactly one of these three (409) in the same call.
"""

EXECUTION_GATES: Final[frozenset[str]] = frozenset({"idempotency", "confidence", "risk_floor"})


class ExecutionReceipt(BaseModel):
    """The real, successful (HTTP 200) result of an `execute_decision` call.

    `extra="allow"` on purpose: the engine may add fields to this envelope over time, and a
    newer engine talking to an older version of this package should not fail to parse just
    because it sent one more field than this model knew about when it was released.
    """

    model_config = ConfigDict(extra="allow")

    decision_id: str
    webhook_url: str
    execution_status: Literal["delivered", "failed"]
    response_code: int | None = None
    executed_at: str | None = None
    policy_snapshot_id: str | None = None
    schema_snapshot_id: str | None = None
    manifest_version: str | None = None
    payload_summary: Any = None
    safety_overridden: bool = False

    def is_delivered(self) -> bool:
        """Whether the downstream webhook delivery itself succeeded.

        Both `True` and `False` here are a *successful* `execute_decision` call -- the engine
        did what was asked and is honestly reporting whether the webhook accepted it. This is
        not a policy denial; see `ExecutionDenial` for that.
        """
        return self.execution_status == "delivered"


class ExecutionDenial(BaseModel):
    """The real, synchronous (HTTP 409) policy-gate denial shape for `execute_decision`.

    Validated against the *inner* `"error"` object of the real response body
    (`{"error": {"code": ..., "gate": ..., "message": ..., "override_hint": ...}}`) -- see
    `parse_denial`, which unwraps that envelope before validating.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    """The full named error code, e.g. `"execution_blocked_confidence"`."""

    gate: str
    """The bare gate name -- one of `EXECUTION_GATES` on every real engine response, though this
    field is left as `str` rather than `ExecutionGate` so a future fourth gate the engine adds
    doesn't fail validation here; it would just not be one of the three this package's own tests
    exercise by name."""

    message: str = ""
    override_hint: str | None = None

    def denial_reason(self) -> str:
        """A human-readable reason, preserving the engine's own gate name and message."""
        reason = f"{self.gate}: {self.message}" if self.message else self.gate
        if self.override_hint:
            reason = f"{reason} ({self.override_hint})"
        return reason


def parse_receipt(
    raw_result: Any, *, model: type[ExecutionReceipt] = ExecutionReceipt
) -> ExecutionReceipt | None:
    """Parse a wrapped MCP tool's raw result into an `ExecutionReceipt`, or `None`.

    Returns `None` (rather than raising) when `raw_result` doesn't validate as a successful
    execution receipt -- e.g. a denial body, a dict missing `decision_id`/`webhook_url`/
    `execution_status`, or a non-dict value entirely (such as `plan_decision`'s freeform result,
    or `get_contract`'s discovery payload). This is the deliberate signal
    `AlgentaToolset.call_tool` uses to pass a non-receipt-shaped tool result through unchanged.

    Args:
        raw_result: The wrapped MCP tool's raw result (typically a dict).
        model: The `ExecutionReceipt` subclass to validate against -- pass
            `AlgentaToolset(receipt_model=...)`'s value through here to honor a caller's typed
            subclass instead of always validating against the base model.
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
    except ValidationError:
        return None


def parse_denial(
    raw_result: Any, *, model: type[ExecutionDenial] = ExecutionDenial
) -> ExecutionDenial | None:
    """Parse a wrapped MCP tool's raw result into an `ExecutionDenial`, or `None`.

    Unwraps the real response body's outer `{"error": {...}}` envelope and validates the inner
    object. Returns `None` for anything that isn't shaped like a denial -- a successful receipt,
    a freeform result from some other tool, or a non-dict value entirely.
    """
    if not isinstance(raw_result, dict):
        return None
    error = raw_result.get("error")
    if not isinstance(error, dict):
        return None
    try:
        return model.model_validate(error)
    except ValidationError:
        return None
