"""pydantic-ai tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a `pydantic_ai.toolsets.wrapper.WrapperToolset`
(`AlgentaToolset`) with tool-profile filtering, a typed `execute_decision` success/denial mapping
(`ExecutionReceipt` / `ExecutionDenial` onto pydantic-ai's `ToolReturnPart.outcome`), and two-layer
`force`/`override_safety` scrubbing.

See the package README for a runnable example, and `contracts/integration-tool-contract.json`
in the `algenta-integrations` repository root for the tool-profile contract this package
conforms to.
"""

from importlib.metadata import PackageNotFoundError, version

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .receipts import (
    EXECUTION_GATES,
    ExecutionDenial,
    ExecutionGate,
    ExecutionReceipt,
    parse_denial,
    parse_receipt,
)
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, AlgentaToolset

try:
    __version__ = version("pydantic-ai-algenta")
except PackageNotFoundError:
    # Not installed (e.g. running from a source checkout without `pip install -e .` /
    # `uv sync`) -- there's no installed distribution for importlib.metadata to read a version
    # from. `pyproject.toml`'s `[project.version]` remains the single source of truth either way.
    __version__ = "0+unknown"

__all__ = [
    "AlgentaToolset",
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "DEFAULT_PROFILE",
    "EXECUTION_GATES",
    "ExecutionDenial",
    "ExecutionGate",
    "ExecutionReceipt",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "__version__",
    "parse_denial",
    "parse_receipt",
]
