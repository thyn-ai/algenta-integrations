"""pydantic-ai tool integration for Algenta.

Wraps a self-hosted Algenta Engine's MCP tool surface as a `pydantic_ai.toolsets.wrapper.WrapperToolset`
(`AlgentaToolset`) with tool-profile filtering, typed governed-execution receipts, and native
mapping onto pydantic-ai's deferred-tool-approval primitives (`ApprovalRequired` /
`DeferredToolRequests` / `DeferredToolResults`) for `execute_decision`'s approval-gated path.

See the package README for a runnable example, and `contracts/integration-tool-contract.json`
in the `algenta-integrations` repository root for the tool-profile contract this package
conforms to.
"""

from .contract import DEFAULT_PROFILE, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES, ToolProfile
from .receipts import ApprovalState, GovernedExecutionReceipt, NAMED_POLICY_GATE_CODES, parse_receipt
from .resume import ApproveCallback, approve_and_resume
from .toolset import ALGENTA_BASE_URL_ENV_VAR, DEFAULT_ALGENTA_BASE_URL, AlgentaToolset

__all__ = [
    "AlgentaToolset",
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "ApprovalState",
    "ApproveCallback",
    "DEFAULT_PROFILE",
    "GovernedExecutionReceipt",
    "NAMED_POLICY_GATE_CODES",
    "NEVER_MODEL_FACING_FIELDS",
    "TOOL_PROFILES",
    "ToolProfile",
    "approve_and_resume",
    "parse_receipt",
]

__version__ = "0.1.0"
