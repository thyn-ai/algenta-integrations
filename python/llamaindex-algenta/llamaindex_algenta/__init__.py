"""LlamaIndex tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware
`list[llama_index.core.tools.FunctionTool]` (`create_algenta_tools`) -- built on
`llama-index-tools-mcp`'s own real `BasicMCPClient`/`McpToolSpec` primitives -- with tool-profile
filtering, two-layer `force`/`override_safety` scrubbing, and a real approval mapping onto
`llama-index-workflows`' `Context.wait_for_event()` human-in-the-loop primitive for a paused
`execute_decision` call.

See the package README for a runnable example and the honest "why" behind the approval-mapping
and receipt-citation design decisions (verified live, not assumed, against installed
`llama-index-core` 0.14.24 / `llama-index-tools-mcp` 0.4.8 / `mcp` 1.29.0), and
`contracts/integration-tool-contract.json` in the `algenta-integrations` repository root for the
tool-profile contract this package conforms to.
"""

from .contract import DEFAULT_PROFILE, GOVERNED_TOOL_NAMES, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import (
    AlgentaApprovalStillPending,
    AlgentaGovernedCallFailure,
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
)
from .receipts import (
    NAMED_POLICY_GATE_CODES,
    ApprovalState,
    GovernedExecutionReceipt,
    call_error_text,
    extract_receipt_from_call_tool_result,
    is_call_error,
    parse_receipt,
    unwrap_call_tool_result,
)
from .toolset import (
    ALGENTA_BASE_URL_ENV_VAR,
    DEFAULT_ALGENTA_BASE_URL,
    DEFAULT_APPROVAL_WAIT_TIMEOUT,
    create_algenta_tools,
)

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_APPROVAL_WAIT_TIMEOUT",
    "DEFAULT_PROFILE",
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
    "ApprovalState",
    "GOVERNED_TOOL_NAMES",
    "GovernedExecutionReceipt",
    "NAMED_POLICY_GATE_CODES",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "call_error_text",
    "create_algenta_tools",
    "extract_receipt_from_call_tool_result",
    "is_call_error",
    "parse_receipt",
    "unwrap_call_tool_result",
]

__version__ = "0.1.0"
