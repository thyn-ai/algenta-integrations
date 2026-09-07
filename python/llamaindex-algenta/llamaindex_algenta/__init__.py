"""LlamaIndex tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as an outcome-aware
`list[llama_index.core.tools.FunctionTool]` (`create_algenta_tools`) -- built on
`llama-index-tools-mcp`'s own real `BasicMCPClient`/`McpToolSpec` primitives -- with tool-profile
filtering, two-layer `force`/`override_safety` scrubbing, and a real, synchronous
receipt/denial outcome mapping for `execute_decision`.

See the package README for a runnable example and the honest "why" behind the execution-outcome
mapping design decisions, and `contracts/integration-tool-contract.json` in the
`algenta-integrations` repository root for the tool-profile contract this package conforms to.
"""

from importlib import metadata as _metadata

from .contract import DEFAULT_PROFILE, GOVERNED_TOOL_NAMES, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import AlgentaGovernedCallFailure, AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import (
    NAMED_EXECUTION_GATES,
    ExecutionDenial,
    ExecutionGate,
    ExecutionReceipt,
    call_error_text,
    is_call_error,
    parse_execution_denial,
    parse_execution_receipt,
    unwrap_call_tool_result,
)
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "AlgentaGovernedCallFailure",
    "AlgentaToolDenied",
    "AlgentaToolExecutionFailed",
    "ExecutionDenial",
    "ExecutionGate",
    "ExecutionReceipt",
    "GOVERNED_TOOL_NAMES",
    "NAMED_EXECUTION_GATES",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "call_error_text",
    "create_algenta_tools",
    "is_call_error",
    "parse_execution_denial",
    "parse_execution_receipt",
    "unwrap_call_tool_result",
]

try:
    # Single source of truth: the version installed metadata reports for this distribution,
    # which setuptools always derives from this package's own `pyproject.toml` `[project].version`
    # at build time. This avoids a second, hand-maintained copy of the version string that can
    # silently drift out of sync with the one `pip`/`uv` actually installed.
    __version__ = _metadata.version("llamaindex-algenta")
except _metadata.PackageNotFoundError:
    # Only reachable when this package is used straight from a source checkout without having
    # been installed (editable or otherwise) -- e.g. running its modules directly off `sys.path`.
    __version__ = "0.0.0+unknown"
