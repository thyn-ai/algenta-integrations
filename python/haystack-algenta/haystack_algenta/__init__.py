"""Haystack tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware
`haystack.tools.toolset.Toolset` (`create_algenta_tools`) -- built on Haystack's own real
`haystack_integrations.tools.mcp.MCPToolset` -- with tool-profile filtering and two-layer
`force`/`override_safety` scrubbing, plus an `Agent` `after_tool` hook
(`build_algenta_governance_hooks`) that raises a typed `AlgentaToolDenied` for the real
`execute_decision` 409 denial (naming one of the three real gates: `"idempotency"`,
`"confidence"`, `"risk_floor"`), via Haystack's own real `after_tool` hook seam.

See the package README for a runnable example and the honest "why" behind the approval/denial
mapping design (verified live, not assumed, against every version this package's `pyproject.toml`
permits -- see the README's "Denial mapping" section), and
`contracts/integration-tool-contract.json` in the `algenta-integrations` repository root for the
tool-profile contract this package conforms to.
"""

from importlib.metadata import PackageNotFoundError, version as _version

from .contract import DEFAULT_PROFILE, GOVERNED_TOOL_NAMES, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import AlgentaGovernedCallFailure, AlgentaToolDenied
from .hooks import GovernedReceiptHook, build_algenta_governance_hooks
from .receipts import (
    ExecutionBlocked,
    ExecutionGate,
    ExecutionReceipt,
    extract_execution_outcome_from_tool_result,
    parse_execution_outcome,
    unwrap_mcp_tool_result,
)
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, AlgentaToolset, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolset",
    "ExecutionBlocked",
    "ExecutionGate",
    "ExecutionReceipt",
    "GOVERNED_TOOL_NAMES",
    "GovernedReceiptHook",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "build_algenta_governance_hooks",
    "create_algenta_tools",
    "extract_execution_outcome_from_tool_result",
    "parse_execution_outcome",
    "unwrap_mcp_tool_result",
]

try:
    __version__ = _version("haystack-algenta")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout, never installed
    __version__ = "0.0.0+unknown"
