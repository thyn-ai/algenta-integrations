"""Temporal.io integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as Temporal activities
(`AlgentaActivities`) with tool-profile enforcement, typed execution receipts
(`ExecutionReceiptData`), and the real `execute_decision` denial mapping onto Temporal's own
failure vocabulary: a blocked governed call raises a non-retryable `ApplicationError` whose
`type` is the engine's denial code and whose details carry the full structured denial body,
recoverable in workflow code via `denial_from_activity_error`.

The governance story for durable execution lives in three layers:

- **Idempotency via receipts**: a retried `execute_decision` activity attempt re-hits the
  engine's own idempotency gate, and the typed `execution_blocked_idempotency` failure is
  positive proof the delivery already landed -- see `recipes/idempotent_activity_receipt_dedup.py`.
- **Structured denials as typed failures**: policy gates are deterministic, so they are
  non-retryable by construction and enumerate cleanly in
  `RetryPolicy(non_retryable_error_types=...)` -- see `recipes/retry_policy_structured_denials.py`.
- **Human approval via signals**: the engine intentionally exposes no MCP approval round trip,
  so approval is a Temporal-native `@workflow.signal` that resumes a paused workflow with an
  operator-controlled `override_safety` -- see `recipes/human_approval_workflow.py`.

See the package README for a runnable quickstart, the recipe index, and the honest "why"
behind this design, and `contracts/integration-tool-contract.json` in the
`algenta-integrations` repository root for the tool-profile contract this package conforms to.
"""

from __future__ import annotations

import importlib.metadata

from .activities import AlgentaActivities
from .client import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, AlgentaMcpClient
from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .errors import (
    AlgentaExecutionBlocked,
    AlgentaGovernedCallError,
    AlgentaToolCallFailed,
    AlgentaToolDenied,
    denial_application_error,
)
from .receipts import (
    NAMED_EXECUTION_GATES,
    ExecutionDenial,
    ExecutionGate,
    ExecutionReceipt,
    parse_denial,
    parse_receipt,
)
from .types import (
    DENIAL_ERROR_TYPES,
    PROFILE_DENIED_ERROR_TYPE,
    SESSION_ERROR_TYPE,
    TOOL_ERROR_TYPE,
    UNEXPECTED_RESULT_ERROR_TYPE,
    ApprovalDecision,
    AuditEvent,
    BatchSimulationReport,
    DenialDetails,
    ExecuteDecisionInput,
    ExecutionReceiptData,
    GovernedDecisionInput,
    LogDecisionInput,
    denial_from_activity_error,
)

try:
    __version__ = importlib.metadata.version("temporal-algenta")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "DENIAL_ERROR_TYPES",
    "NAMED_EXECUTION_GATES",
    "NEVER_MODEL_FACING_FIELDS",
    "PROFILE_DENIED_ERROR_TYPE",
    "SESSION_ERROR_TYPE",
    "TOOL_ERROR_TYPE",
    "TOOL_PROFILES",
    "UNEXPECTED_RESULT_ERROR_TYPE",
    "AlgentaActivities",
    "AlgentaExecutionBlocked",
    "AlgentaGovernedCallError",
    "AlgentaMcpClient",
    "AlgentaToolCallFailed",
    "AlgentaToolDenied",
    "ApprovalDecision",
    "AuditEvent",
    "BatchSimulationReport",
    "DenialDetails",
    "ExecuteDecisionInput",
    "ExecutionDenial",
    "ExecutionGate",
    "ExecutionReceipt",
    "ExecutionReceiptData",
    "GovernedDecisionInput",
    "LogDecisionInput",
    "ToolProfile",
    "__version__",
    "denial_application_error",
    "denial_from_activity_error",
    "parse_denial",
    "parse_receipt",
]
