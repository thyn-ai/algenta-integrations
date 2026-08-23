"""LangChain / LangGraph tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware list of
LangChain `BaseTool`s (`create_algenta_tools`) with tool-profile filtering, typed
governed-execution receipts, and native mapping onto LangGraph's own human-in-the-loop pause
primitive (`langgraph.types.interrupt` / `Command(resume=...)`) for `execute_decision`'s
approval-gated path.

See the package README for a runnable example and for the honest "why" behind the
approval-mapping design, and `contracts/integration-tool-contract.json` in the
`algenta-integrations` repository root for the tool-profile contract this package conforms to.
"""

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import (
    AlgentaApprovalStillPending,
    AlgentaGovernedCallError,
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
)
from .interceptor import AlgentaToolCallInterceptor
from .receipts import ApprovalState, GovernedExecutionReceipt, NAMED_POLICY_GATE_CODES, parse_receipt
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallError",
    "AlgentaToolCallInterceptor",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
    "ApprovalState",
    "GovernedExecutionReceipt",
    "NAMED_POLICY_GATE_CODES",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "create_algenta_tools",
    "parse_receipt",
]

__version__ = "0.1.0"
