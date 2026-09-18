"""The 12 cross-framework conformance scenarios, and an honest record of which can run.

WHY THIS FILE STATES WHAT IT CANNOT DO. Every integration package in this repo was labelled
"conformance-verified" while this suite did not exist, and the only file carrying the name
(`python/litellm-algenta/tests/test_gateway_conformance.py`) runs against a `stub_server.py`. That
is how a claim outruns its evidence: the shared gate was planned, the label was applied, and nothing
connected the two.

So each scenario below carries an explicit `status`:

  EXERCISABLE      — runs against a real engine and asserts real behaviour.
  NEEDS_CAPABILITY — the engine does not implement this yet. Named, not skipped.
  NEEDS_INFRA      — the behaviour exists but this environment cannot produce the trigger.

A scenario that cannot run is reported as such by the runner, with its reason, and does NOT count
toward a pass. `pytest.skip` was deliberately avoided: a skipped test disappears into a summary line
and "10 passed, 2 skipped" reads like success.

MEASURED against the engine's public HTTP API in internal CI (over HTTP, not the test client,
with a real Postgres): 9 of 12 are exercisable today. The three that are not are named below
with the specific reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Status(StrEnum):
    EXERCISABLE = "exercisable"
    NEEDS_CAPABILITY = "needs_capability"
    NEEDS_INFRA = "needs_infra"


@dataclass(frozen=True)
class Scenario:
    number: int
    key: str
    title: str
    status: Status
    # What the engine must actually do. For EXERCISABLE scenarios this is what the runner asserts.
    expectation: str
    # For the others: precisely what is missing, so it is actionable rather than a shrug.
    blocked_reason: str = ""
    # Named error code the engine returns, where one applies. Recorded from a live run rather than
    # assumed -- see scenario 7, whose real code is not the one the plan's wording implies.
    expected_code: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        1, "read_only_recommendation",
        "Successful read-only recommendation",
        Status.EXERCISABLE,
        "A decision case accepts input, an analysis run completes, and a plan is proposed carrying "
        "a plan_hash and a single-use nonce.",
        tags=("read", "happy-path"),
    ),
    Scenario(
        2, "execution_denied_by_policy",
        "Execution denied by policy (409 named gate)",
        Status.EXERCISABLE,
        "Executing an unapproved plan returns 409 with the NAMED code, not a generic http_error.",
        expected_code="plan_not_approved",
        tags=("govern", "negative"),
    ),
    Scenario(
        3, "execution_paused_for_approval",
        "Execution paused for approval",
        Status.NEEDS_CAPABILITY,
        "An execute attempt that hits an approval gate emits an approval-required event and can be "
        "resumed with a continuation token.",
        blocked_reason=(
            "The engine REFUSES rather than PAUSES. /v1/decision-plans/{id}/execute on an "
            "unapproved plan returns 409 plan_not_approved and the plan stays 'proposed' -- there "
            "is no suspended execution to resume, and no continuation token. Approval is a "
            "separate prior call (propose -> approve -> execute), which is a sound design but is "
            "not the pause/resume semantics this scenario describes. Interruption + continuation "
            "is planned engine work (a Responses-protocol approval-required event); asserting it "
            "now would be asserting a design intention."
        ),
        tags=("govern", "approval"),
    ),
    Scenario(
        4, "approval_granted_then_execute",
        "Approval granted, execution resumed",
        Status.EXERCISABLE,
        "Approving with the exact plan_hash and nonce moves the plan to approved; execute then "
        "succeeds and returns a receipt.",
        tags=("govern", "happy-path"),
    ),
    Scenario(
        5, "approval_rejected",
        "Approval rejected",
        Status.EXERCISABLE,
        "Approving with a wrong nonce is refused by name and does not advance the plan.",
        expected_code="invalid_nonce",
        tags=("govern", "negative"),
    ),
    Scenario(
        6, "duplicate_execution_prevented",
        "Duplicate execution prevented",
        Status.EXERCISABLE,
        "Executing an already-executed plan is refused. The engine uses a compare-and-set on the "
        "'approved' state, so the second caller loses rather than both succeeding.",
        expected_code="plan_not_approved",
        tags=("govern", "idempotency"),
    ),
    Scenario(
        7, "reused_challenge_rejected",
        "Expired / reused approval rejected",
        Status.EXERCISABLE,
        "The approval nonce is single-use: presenting it twice is refused.",
        # Recorded live, and NOT what the plan's wording implies. After a successful approval the
        # plan leaves 'proposed', so the lifecycle gate answers before the nonce is ever compared.
        # The nonce IS single-use; the code just names the earlier gate. Asserting invalid_nonce
        # here would have been asserting a guess.
        expected_code="plan_not_approvable",
        tags=("govern", "negative"),
    ),
    Scenario(
        8, "modified_plan_hash_rejected",
        "Modified plan_hash rejected",
        Status.EXERCISABLE,
        "Approving with a plan_hash that does not match the proposal is refused by name -- the "
        "approval is bound to the exact plan that was reviewed.",
        expected_code="plan_hash_mismatch",
        tags=("govern", "negative", "integrity"),
    ),
    Scenario(
        9, "upstream_timeout_typed_retryable",
        "Upstream timeout surfaces as a typed retryable error",
        Status.NEEDS_INFRA,
        "A timing-out dependency produces an error whose envelope marks retryable=true, so a "
        "caller can distinguish it from a policy denial.",
        blocked_reason=(
            "Requires a dependency that actually times out. The governed path exercised here talks "
            "only to Postgres; provoking a real timeout means either a fault-injection proxy in "
            "front of an upstream or the runtime compute worker under load, neither of which this "
            "environment has. Fabricating one by patching a client would test the mock, not the "
            "engine. Belongs in the nightly live tier against a booted stack."
        ),
        tags=("errors", "retry"),
    ),
    Scenario(
        10, "execution_receipt_versioned",
        "Successful execution receipt (versioned envelope)",
        Status.EXERCISABLE,
        "The receipt carries receipt_version, a distinct execution_id, the bound plan_hash, "
        "approval_state, and the caller's trace_id and idempotency_key.",
        tags=("receipts", "happy-path"),
    ),
    Scenario(
        11, "audit_export_hash_verifies",
        "Audit export whose X-Content-SHA256 verifies",
        Status.EXERCISABLE,
        "GET /v1/audit-logs/export.{fmt} returns X-Content-SHA256 equal to sha256 of the delivered "
        "bytes, re-hashed by the client exactly as an auditor would.",
        tags=("evidence", "audit"),
    ),
    Scenario(
        12, "replay_is_deterministic",
        "Replay produces the same deterministic result",
        Status.NEEDS_INFRA,
        "Re-running a recorded execution from its replay manifest yields a byte-identical result.",
        blocked_reason=(
            "Determinism is a property of the engine's compute kernels, and this environment runs "
            "with the runtime compute worker disabled -- the governed-execution semantics under "
            "test here do not need the worker, but replay identity does. Comparing two runs of "
            "the current execute path would compare an artifact list to itself and prove nothing "
            "about determinism. Belongs in the nightly live tier with the worker running."
        ),
        tags=("evidence", "determinism"),
    ),
)

BY_KEY = {s.key: s for s in SCENARIOS}
EXERCISABLE = tuple(s for s in SCENARIOS if s.status is Status.EXERCISABLE)
BLOCKED = tuple(s for s in SCENARIOS if s.status is not Status.EXERCISABLE)


def summary() -> str:
    return (
        f"{len(SCENARIOS)} scenarios: {len(EXERCISABLE)} exercisable, "
        f"{len(BLOCKED)} blocked ("
        + ", ".join(f"#{s.number} {s.status.value}" for s in BLOCKED)
        + ")"
    )
