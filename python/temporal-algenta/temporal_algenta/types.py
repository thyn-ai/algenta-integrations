"""Workflow-wire types for `temporal_algenta`: stdlib-only dataclasses that cross the
Temporal workflow <-> activity boundary, plus the workflow-side helpers for turning a failed
activity's typed `ApplicationError` back into a structured denial.

Everything in this module is deliberately importable inside Temporal's workflow sandbox: it
imports only the standard library and `temporalio` itself (always passed through by the
sandbox). The pydantic receipt/denial models live in `temporal_algenta.receipts` and are used
at the activity edge only -- pydantic's Rust core cannot be reloaded by the workflow sandbox,
so workflow code must never import that module. Activities validate engine payloads into the
pydantic models (strict edge validation), then copy them into these plain dataclasses for the
wire (Temporal's default data converter serializes dataclasses natively).

Determinism note: every field here is plain data. Nothing in this module reads the clock,
randomness, or the environment, so these types are safe to construct and inspect from workflow
code under replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from temporalio.exceptions import ApplicationError

#: The three real `execute_decision` denial codes, also used as the Temporal `ApplicationError`
#: `type` strings this package raises for a blocked governed call (see
#: `temporal_algenta.errors.denial_application_error`). Listing them in a workflow's activity
#: `RetryPolicy(non_retryable_error_types=[...])` is the explicit, self-documenting way to keep
#: policy denials from being retried; the package already marks these errors
#: `non_retryable=True`, so the listing is belt-and-braces (see
#: `recipes/retry_policy_structured_denials.py`).
DENIAL_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {"execution_blocked_idempotency", "execution_blocked_confidence", "execution_blocked_risk_floor"}
)

#: `ApplicationError.type` for a call refused *client-side* because the tool is outside the
#: configured tool profile (see `temporal_algenta.contract`). Never emitted by the engine
#: itself; always `non_retryable` -- retrying the same tool under the same profile can only
#: refuse again.
PROFILE_DENIED_ERROR_TYPE: Final = "algenta_tool_denied_outside_profile"

#: `ApplicationError.type` for a non-denial MCP tool-execution error (`isError=True` whose body
#: is not one of the three named policy gates -- e.g. a transient engine-side failure).
#: Deliberately retryable: unlike a policy denial, an unclassified tool error may clear on
#: retry, and Temporal's retry policy is the right place to bound that (see
#: `recipes/retry_policy_structured_denials.py`).
TOOL_ERROR_TYPE: Final = "algenta_tool_error"

#: `ApplicationError.type` for an MCP *session-level* failure (`McpError` -- a read timeout on
#: a wedged session, a session-establishment failure) as opposed to an error the engine's tool
#: reported (`TOOL_ERROR_TYPE`). Also retryable -- a fresh session almost always clears it --
#: but typed separately so tests and operators can tell "the engine answered with an error"
#: apart from "the session never got an answer".
SESSION_ERROR_TYPE: Final = "algenta_session_error"

#: `ApplicationError.type` for an `execute_decision` call that completed without error but
#: whose payload did not validate as an `ExecutionReceipt` -- a contract violation by the
#: connected server, never something a retry can fix, so always `non_retryable`.
UNEXPECTED_RESULT_ERROR_TYPE: Final = "algenta_unexpected_execute_decision_result"


@dataclass(frozen=True)
class ExecuteDecisionInput:
    """The operator-facing input of the `execute_decision` activity.

    `force` / `override_safety` are present here on purpose: this is workflow-author-facing
    code, not a model-facing tool schema. The shared contract reserves those two fields for
    "operator/break-glass use only ... outside the model-facing tool call entirely"
    (`contracts/integration-tool-contract.json`), and a Temporal workflow is exactly such an
    operator-controlled path -- e.g. set from a human approval signal (see
    `recipes/human_approval_workflow.py`). They default to `False` and are never set
    implicitly.
    """

    decision_id: str
    webhook_url: str
    timeout_seconds: float = 30.0
    force: bool = False
    override_safety: bool = False
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class GovernedDecisionInput:
    """The common "log this decision, then execute it against this webhook" workflow input
    shared by several recipes. `action` becomes the logged decision's `chosen_action`; the demo
    engine (and the real contract's `log_decision` -> `execute_decision` flow) derives the
    `decision_id` from it."""

    action: str
    webhook_url: str
    rationale: str | None = None


@dataclass(frozen=True)
class LogDecisionInput:
    """Input of the `log_decision` activity: persist a decision record (and its rationale) to
    decision memory; returns the engine's decision record dict carrying the `decision_id` every
    later `execute_decision` call needs."""

    chosen_action: str
    rationale: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ExecutionReceiptData:
    """The wire copy of an `execute_decision` success envelope -- field-for-field the pydantic
    `temporal_algenta.receipts.ExecutionReceipt`, as a plain dataclass so it survives Temporal's
    default data converter and the workflow sandbox.

    `execution_status == "failed"` is still a *successful call*: the engine delivered (or tried
    to deliver) and returned this receipt; only the downstream webhook target rejected it. A
    policy denial never appears here -- denials cross the wire as typed `ApplicationError`s
    instead (see `DenialDetails`).
    """

    decision_id: str
    webhook_url: str
    execution_status: str
    response_code: int
    executed_at: str
    policy_snapshot_id: str
    schema_snapshot_id: str
    manifest_version: str
    payload_summary: Any = None
    safety_overridden: bool = False

    def is_delivered(self) -> bool:
        """Whether the webhook delivery itself landed (`execution_status == "delivered"`)."""
        return self.execution_status == "delivered"


@dataclass(frozen=True)
class DenialDetails:
    """The wire copy of a policy denial -- the engine's `{"code", "gate", "message",
    "override_hint"}` body as a plain dataclass.

    Raised inside an `ApplicationError`'s `details` (as a plain dict, see `to_json_dict`) by the
    `execute_decision` activity; recovered on the workflow side by
    `denial_from_activity_error`.
    """

    code: str
    gate: str
    message: str
    override_hint: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        """The plain-dict shape placed into `ApplicationError.details` (JSON-serializable under
        Temporal's default failure converter, and tolerant of older engines omitting
        `override_hint`)."""
        return {"code": self.code, "gate": self.gate, "message": self.message, "override_hint": self.override_hint}

    @classmethod
    def from_json_dict(cls, value: Any) -> DenialDetails | None:
        """Parse one `ApplicationError.details` element back into a `DenialDetails`, or `None`
        if it isn't one (e.g. details of some unrelated application error)."""
        if not isinstance(value, dict):
            return None
        code, gate, message = value.get("code"), value.get("gate"), value.get("message")
        if not (isinstance(code, str) and isinstance(gate, str) and isinstance(message, str)):
            return None
        override_hint = value.get("override_hint")
        return cls(code=code, gate=gate, message=message, override_hint=override_hint if isinstance(override_hint, str) else None)


def denial_from_activity_error(error: BaseException) -> DenialDetails | None:
    """Recover the structured policy denial from a failed activity's exception chain, or `None`.

    Walks the `__cause__` chain (a failed Temporal activity surfaces in workflow code as an
    `ActivityError` whose cause is the activity's own exception) looking for an
    `ApplicationError` that carries a `DenialDetails` dict in its details. Returns `None` for
    anything else -- a transport failure, a `PROFILE_DENIED_ERROR_TYPE` profile refusal (those
    carry no denial body), an unexpected-shape failure -- so workflow code can tell "the engine
    denied this on a named policy gate" apart from every other failure mode:

        try:
            receipt = await workflow.execute_activity(...)
        except ActivityError as err:
            denial = denial_from_activity_error(err)
            if denial is None:
                raise  # not a policy denial -- let the workflow fail
            ...
    """
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ApplicationError):
            for detail in current.details:
                denial = DenialDetails.from_json_dict(detail)
                if denial is not None:
                    return denial
        current = current.__cause__
    return None


@dataclass
class ApprovalDecision:
    """One operator's verdict, delivered to a waiting workflow via signal (see
    `recipes/human_approval_workflow.py`). Not frozen only because signal payloads are
    deserialized fresh per call; nothing in a workflow mutates a received one."""

    approved: bool
    operator: str
    reason: str = ""
    override_safety: bool = False
    """Whether the operator is explicitly authorizing the `override_safety` break-glass for the
    retry after this approval. Defaults to `False`; only a human signal may set it `True`."""


@dataclass
class AuditEvent:
    """One deterministic audit-trail entry recorded by a workflow as it makes governed calls
    (see `recipes/audit_trail_query_workflow.py`). `seq` is the workflow-owned sequence number
    (deterministic under replay -- no clocks, no randomness)."""

    seq: int
    kind: str
    detail: str
    decision_id: str | None = None


@dataclass
class BatchSimulationReport:
    """The aggregate result of a fan-out of `simulate` activities (see
    `recipes/batch_simulation_pipeline.py`). `results` preserves input order (one entry per
    requested scenario) so the report is fully deterministic."""

    scenario_count: int
    succeeded: int
    expected_values: dict[str, float] = field(default_factory=dict)
    mean_expected_value: float = 0.0


__all__ = [
    "DENIAL_ERROR_TYPES",
    "PROFILE_DENIED_ERROR_TYPE",
    "TOOL_ERROR_TYPE",
    "UNEXPECTED_RESULT_ERROR_TYPE",
    "ApprovalDecision",
    "AuditEvent",
    "BatchSimulationReport",
    "DenialDetails",
    "ExecuteDecisionInput",
    "ExecutionReceiptData",
    "GovernedDecisionInput",
    "LogDecisionInput",
    "denial_from_activity_error",
]
