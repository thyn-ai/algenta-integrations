"""Haystack tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware
`haystack.tools.toolset.Toolset` (`create_algenta_tools`) -- built on Haystack's own real
`haystack_integrations.tools.mcp.MCPToolset` -- with tool-profile filtering and two-layer
`force`/`override_safety` scrubbing, plus an `Agent` `before_tool`/`after_tool` hook pair
(`build_algenta_governance_hooks`) mapping the governed-execution receipt contract onto Haystack's
own real `ConfirmationHook` human-in-the-loop primitive and its `after_tool` hook seam.

See the package README for a runnable example and the honest "why" behind the approval-mapping
design (verified live, not assumed, against installed `haystack-ai` 3.0.0 / `mcp-haystack` 1.4.1),
and `contracts/integration-tool-contract.json` in the `algenta-integrations` repository root for
the tool-profile contract this package conforms to.
"""

from .contract import DEFAULT_PROFILE, GOVERNED_TOOL_NAMES, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import (
    AlgentaApprovalStillPending,
    AlgentaGovernedCallFailure,
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
)
from .hooks import AlgentaGovernanceHooks, GovernedReceiptHook, build_algenta_governance_hooks, default_confirmation_hook
from .receipts import (
    NAMED_POLICY_GATE_CODES,
    ApprovalState,
    GovernedExecutionReceipt,
    extract_receipt_from_tool_result,
    parse_receipt,
    unwrap_mcp_tool_result,
)
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, AlgentaToolset, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "AlgentaApprovalStillPending",
    "AlgentaGovernanceHooks",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
    "AlgentaToolset",
    "ApprovalState",
    "GOVERNED_TOOL_NAMES",
    "GovernedExecutionReceipt",
    "GovernedReceiptHook",
    "NAMED_POLICY_GATE_CODES",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "build_algenta_governance_hooks",
    "create_algenta_tools",
    "default_confirmation_hook",
    "extract_receipt_from_tool_result",
    "parse_receipt",
    "unwrap_mcp_tool_result",
]

__version__ = "0.1.0"
