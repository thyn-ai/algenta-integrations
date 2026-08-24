"""Microsoft Agent Framework (MAF) tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware list of
`agent_framework.FunctionTool`s (`create_algenta_tools`) with tool-profile filtering, a typed
`execute_decision` receipt/denial mapping, and `agent_framework.MiddlewareFailure` (MAF's one
fail-closed abort primitive) for the real, synchronous 409 denial `execute_decision` can raise.

See the package README for a runnable example and the honest "why" behind the denial-mapping
design, and `contracts/integration-tool-contract.json` in the `algenta-integrations` repository
root for the tool-profile contract this package conforms to.

This package is about **Microsoft Agent Framework** specifically: a real, open-source,
pip-installable SDK (`agent-framework` on PyPI) that runs standalone against any self-hosted MCP
endpoint, no Azure account or Foundry project required. It is not about **Microsoft Foundry**
(the hosted Azure platform) -- see `foundry/README.md` at the repository root of this package for
that separate, documentation-only, explicitly-not-independently-verified deliverable.
"""

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import AlgentaGovernedCallFailure, AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import (
    ExecutionBlocked,
    ExecutionGate,
    ExecutionReceipt,
    parse_execution_blocked,
    parse_execution_receipt,
)
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
    "ExecutionBlocked",
    "ExecutionGate",
    "ExecutionReceipt",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "create_algenta_tools",
    "parse_execution_blocked",
    "parse_execution_receipt",
]

__version__ = "0.1.0"
