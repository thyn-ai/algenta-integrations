"""LangChain / LangGraph tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a governed-execution-aware list of
LangChain `BaseTool`s (`create_algenta_tools`) with tool-profile filtering, typed execution
receipts/denials, and a real `execute_decision` denial mapping: the engine decides
success-vs-blocked *synchronously, in the same call* (never a separate "pending approval" round
trip), so a blocked call surfaces as a normal, catchable LangChain tool-call error
(`AlgentaExecutionBlocked`), not a paused run.

See the package README for a runnable example and for the honest "why" behind this design, and
`contracts/integration-tool-contract.json` in the `algenta-integrations` repository root for the
tool-profile contract this package conforms to.
"""

import importlib.metadata

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .exceptions import AlgentaExecutionBlocked, AlgentaGovernedCallError, AlgentaToolDenied
from .interceptor import AlgentaToolCallInterceptor
from .receipts import (
    NAMED_EXECUTION_GATES,
    ExecutionDenial,
    ExecutionGate,
    ExecutionReceipt,
    parse_denial,
    parse_receipt,
)
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, create_algenta_tools

__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "NAMED_EXECUTION_GATES",
    "AlgentaExecutionBlocked",
    "AlgentaGovernedCallError",
    "AlgentaToolCallInterceptor",
    "AlgentaToolDenied",
    "ExecutionDenial",
    "ExecutionGate",
    "ExecutionReceipt",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "create_algenta_tools",
    "parse_denial",
    "parse_receipt",
]

try:
    __version__ = importlib.metadata.version("langchain-algenta")
except importlib.metadata.PackageNotFoundError:
    # Not installed (e.g. running from a source checkout without `pip install -e .`) -- fall
    # back to a clearly-unresolved marker rather than a stale, hand-maintained number that would
    # silently drift from `pyproject.toml`'s real `version =`, as it once did.
    __version__ = "0.0.0+unknown"
