"""Microsoft Agent Framework (MAF) tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware list of
`agent_framework.FunctionTool`s (`create_algenta_tools`) with tool-profile filtering, typed
governed-execution receipts, and a mapping onto MAF's own real primitives -- `approval_mode`
(pre-call human-in-the-loop) and `agent_framework.MiddlewareFailure` (fail-closed abort) -- for
`execute_decision`'s approval-gated path.

See the package README for a runnable example and the honest "why" behind the approval-mapping
design, and `contracts/integration-tool-contract.json` in the `algenta-integrations` repository
root for the tool-profile contract this package conforms to.

This package is about **Microsoft Agent Framework** specifically: a real, open-source,
pip-installable SDK (`agent-framework` on PyPI) that runs standalone against any self-hosted MCP
endpoint, no Azure account or Foundry project required. It is not about **Microsoft Foundry**
(the hosted Azure platform) -- see `foundry/README.md` at the repository root of this package for
that separate, documentation-only, explicitly-not-independently-verified deliverable.
"""

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import (
    AlgentaApprovalStillPending,
    AlgentaGovernedCallFailure,
    AlgentaToolDenied,
    AlgentaToolExecutionFailed,
)
from .receipts import ApprovalState, GovernedExecutionReceipt, NAMED_POLICY_GATE_CODES, parse_receipt
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "AlgentaApprovalStillPending",
    "AlgentaGovernedCallFailure",
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
