"""`AlgentaToolset` -- a governed-execution-aware `pydantic_ai.toolsets.wrapper.WrapperToolset`
wrapping an `MCPToolset` pointed at the caller's own self-hosted Algenta Engine.

Structural template: `pydantic_ai.toolsets.approval_required.ApprovalRequiredToolset`, the
built-in precedent for "a `WrapperToolset` that raises `ApprovalRequired` from `call_tool`".
`AlgentaToolset` follows the same shape, but the approval decision comes from the wrapped MCP
tool's own result envelope (`GovernedExecutionReceipt.approval_state`) rather than from a
caller-supplied predicate evaluated before the call happens.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.tools import AgentDepsT, RunContext, ToolDenied
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from .contract import NEVER_MODEL_FACING_FIELDS, DEFAULT_PROFILE, ToolProfile, resolve_profile_tool_names
from .receipts import GovernedExecutionReceipt, parse_receipt

if TYPE_CHECKING:
    from pydantic_ai.mcp import MCPToolsetClient

#: Default self-hosted Algenta MCP endpoint. Matches this whole program's standing rule: every
#: default in this repository points at the caller's own self-hosted deployment, never a
#: hosted-by-Algenta cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=`
#: constructor argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"


def _strip_never_model_facing_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return `schema` with `NEVER_MODEL_FACING_FIELDS` removed from `properties`/`required`.

    Returns `schema` itself, unchanged, when none of those fields are present -- so callers can
    cheaply tell via `is` whether anything actually changed.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not (NEVER_MODEL_FACING_FIELDS & properties.keys()):
        return schema
    new_schema = dict(schema)
    new_schema["properties"] = {k: v for k, v in properties.items() if k not in NEVER_MODEL_FACING_FIELDS}
    required = schema.get("required")
    if isinstance(required, list):
        new_schema["required"] = [name for name in required if name not in NEVER_MODEL_FACING_FIELDS]
    return new_schema


def _strip_never_model_facing_tool(tool: ToolsetTool[AgentDepsT]) -> ToolsetTool[AgentDepsT]:
    """Defense-in-depth companion to the `call_tool`-side argument scrub below.

    `contracts/integration-tool-contract.json`'s `never_model_facing_note` says `force` /
    `override_safety` must never reach the model "not in a tool schema, not via a kwarg the
    model's function-call arguments can reach". Stripping the argument dict in `call_tool`
    handles the second half; this handles the first -- if a misconfigured or future server ever
    advertised one of these fields on a tool's schema, the model would never see it as an
    available parameter to begin with.
    """
    new_schema = _strip_never_model_facing_schema(tool.tool_def.parameters_json_schema)
    if new_schema is tool.tool_def.parameters_json_schema:
        return tool
    return dataclasses.replace(tool, tool_def=dataclasses.replace(tool.tool_def, parameters_json_schema=new_schema))


@dataclass
class AlgentaToolset(WrapperToolset[AgentDepsT]):
    """Wraps an `MCPToolset` pointed at a self-hosted Algenta Engine with governed-execution awareness.

    Constructs its own inner `MCPToolset` -- you don't build one yourself and hand it over (pass
    `base_url=`/`auth=`/`headers=`/`http_client=` to configure it instead) -- and layers three
    things on top of the wrapped toolset's real MCP tool calls:

    1. **Tool-profile filtering** (`profile=`): only the tool names
       [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json)
       assigns to the requested profile are exposed to the model. Defaults to `"observe"`
       (read-only), matching the contract's own stated default.
    2. **Typed governed-execution receipts**: every wrapped tool call's raw result is parsed
       into a [`GovernedExecutionReceipt`][pydantic_ai_algenta.receipts.GovernedExecutionReceipt]
       when it validates as one, so message history carries a typed object instead of a raw
       dict. A tool whose result doesn't validate as a receipt (e.g. `get_contract`'s discovery
       payload) passes through unchanged.
    3. **The approval/denial/failure mapping** (see `call_tool` below) between the receipt's
       `approval_state`/`code`/`status` fields and pydantic-ai's own deferred-tool-approval and
       `ToolReturnPart.outcome` mechanisms.

    Example:

    ```python {test="skip"}
    from pydantic_ai import Agent
    from pydantic_ai_algenta import AlgentaToolset

    toolset = AlgentaToolset(base_url="http://localhost:8000/mcp", profile="observe")
    agent = Agent("openai:gpt-5", toolsets=[toolset])
    ```
    """

    profile: ToolProfile = DEFAULT_PROFILE
    receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt

    def __init__(
        self,
        *,
        base_url: str | None = None,
        profile: ToolProfile = DEFAULT_PROFILE,
        wrapped: AbstractToolset[AgentDepsT] | None = None,
        receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt,
        id: str | None = None,
        auth: httpx.Auth | Literal["oauth"] | str | None = None,
        headers: dict[str, str] | None = None,
        http_client: httpx.AsyncClient | None = None,
        **mcp_kwargs: Any,
    ) -> None:
        """Build a new `AlgentaToolset`.

        Args:
            base_url: The self-hosted Algenta MCP endpoint to connect to. Defaults to the
                `ALGENTA_BASE_URL` environment variable, falling back to
                `"http://localhost:8000/mcp"` (self-hosted-first: never a hosted-by-Algenta
                cloud default). Ignored if `wrapped` is given.
            profile: Which tool profile to expose to the model -- one of `"observe"` (default),
                `"govern"`, `"execute"`, or `"full"`. See
                [`contracts/integration-tool-contract.json`](https://github.com/thyn-ai/algenta-integrations/blob/main/contracts/integration-tool-contract.json).
            wrapped: Advanced escape hatch / test seam: pass an already-built toolset (typically
                an `MCPToolset`, or a `TestModel`-friendly stand-in) instead of having
                `AlgentaToolset` build one from `base_url`. Mutually exclusive with `base_url`,
                `auth`, `headers`, `http_client`, and any `**mcp_kwargs`.
            receipt_model: The `GovernedExecutionReceipt` subclass to validate tool results
                against. Override if your engine's receipt envelope has grown fields you want
                typed (the base model already accepts and preserves unknown fields via
                `extra="allow"`, so most callers won't need this).
            id: Forwarded to the constructed `MCPToolset` as its `id`.
            auth: Forwarded to the constructed `MCPToolset` (HTTP auth for the self-hosted
                endpoint).
            headers: Forwarded to the constructed `MCPToolset` (extra HTTP headers).
            http_client: Forwarded to the constructed `MCPToolset` (a pre-configured
                `httpx.AsyncClient`).
            **mcp_kwargs: Any other `MCPToolset` constructor keyword argument (e.g.
                `tool_error_behavior`, `sampling_model`, `cache_tools`), forwarded as-is.

        Raises:
            ValueError: If `profile` isn't one of the four contract profiles, or if both
                `wrapped` and any MCP-connection argument are given.
        """
        from .contract import TOOL_PROFILES  # local import: avoids a module-level cycle risk

        if profile not in TOOL_PROFILES:
            raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")

        connection_kwargs_given = bool(mcp_kwargs) or any(
            value is not None for value in (auth, headers, http_client)
        ) or base_url is not None
        if wrapped is not None and connection_kwargs_given:
            raise ValueError(
                "Pass either `wrapped` or the MCP-connection arguments (`base_url`, `auth`, "
                "`headers`, `http_client`, ...), not both."
            )

        self.profile = profile
        self.receipt_model = receipt_model

        if wrapped is not None:
            self.wrapped = wrapped
            return

        from pydantic_ai.mcp import MCPToolset

        resolved_base_url = base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
        client: MCPToolsetClient = resolved_base_url
        self.wrapped = MCPToolset(
            client,
            id=id,
            auth=auth,
            headers=headers,
            http_client=http_client,
            **mcp_kwargs,
        )

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        """The wrapped toolset's tools, filtered to `self.profile` and scrubbed of never-model-facing fields."""
        tools = await super().get_tools(ctx)
        allowed_names = resolve_profile_tool_names(self.profile, available_tool_names=frozenset(tools))
        return {name: _strip_never_model_facing_tool(tool) for name, tool in tools.items() if name in allowed_names}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> Any:
        """Call the wrapped tool, then map its governed-execution receipt onto pydantic-ai's primitives.

        - `approval_state == "pending"` -> raises
          [`ApprovalRequired`][pydantic_ai.exceptions.ApprovalRequired] with the plan's
          identifying fields in `metadata`, diverting the call into pydantic-ai's own
          deferred-tool-approval flow (`DeferredToolRequests` / `DeferredToolResults`) rather
          than returning it as an ordinary result. See the package README's "Why not
          `outcome='interrupted'`?" note for why this -- not a `ToolReturnPart` outcome -- is the
          correct binding for a paused governed execution.
        - `approval_state in ("rejected", "expired")`, or a named policy-gate `code` (e.g.
          `plan_hash_mismatch`) -- returns [`ToolDenied`][pydantic_ai.tools.ToolDenied], which
          `pydantic-ai` turns into `ToolReturnPart(outcome="denied")` with the engine's own
          code/message preserved (see `GovernedExecutionReceipt.denial_reason`).
        - Anything else that isn't a recognized success -- raises
          [`ToolFailed`][pydantic_ai.exceptions.ToolFailed], turned into
          `ToolReturnPart(outcome="failed")`.
        - A successful call, or a result that doesn't parse as a governed-execution receipt at
          all (e.g. `get_contract`'s discovery payload) -- returned as-is, becoming
          `ToolReturnPart(outcome="success")` (the default).

        `force`/`override_safety` are stripped from `tool_args` before the wrapped call, as a
        second layer behind the schema-level scrub in `get_tools` -- see
        `contracts/integration-tool-contract.json`'s `never_model_facing_note`.
        """
        scrubbed_args = {k: v for k, v in tool_args.items() if k not in NEVER_MODEL_FACING_FIELDS}
        raw_result = await super().call_tool(name, scrubbed_args, ctx, tool)

        receipt = parse_receipt(raw_result, model=self.receipt_model)
        if receipt is None:
            return raw_result

        if receipt.is_pending_approval():
            raise ApprovalRequired(metadata=self._approval_metadata(name, receipt))

        if receipt.is_denied():
            return ToolDenied(message=receipt.denial_reason())

        if not receipt.is_success():
            raise ToolFailed(
                f"{receipt.code}: Algenta tool {name!r} did not complete successfully "
                f"(status={receipt.status!r})."
            )

        return receipt

    @staticmethod
    def _approval_metadata(tool_name: str, receipt: GovernedExecutionReceipt) -> dict[str, Any]:
        """Metadata attached to `ApprovalRequired`, surfaced on `DeferredToolRequests.metadata`.

        Carries everything `approve_and_resume` (see `pydantic_ai_algenta.resume`) -- or a
        caller building their own approval flow -- needs to call the engine's real approval
        endpoint and then resume the run: `plan_hash`, `execution_id`, and `idempotency_key`
        (which doubles as `execute_decision`'s single-use replay nonce), plus the full receipt
        for anything else the caller might want.
        """
        return {
            "tool_name": tool_name,
            "plan_hash": receipt.plan_hash,
            "execution_id": receipt.execution_id,
            "idempotency_key": receipt.idempotency_key,
            "policy_snapshot_hash": receipt.policy_snapshot_hash,
            "request_id": receipt.request_id,
            "trace_id": receipt.trace_id,
            "receipt": receipt.model_dump(),
        }


__all__ = ["AlgentaToolset", "DEFAULT_ALGENTA_BASE_URL", "ALGENTA_BASE_URL_ENV_VAR"]
