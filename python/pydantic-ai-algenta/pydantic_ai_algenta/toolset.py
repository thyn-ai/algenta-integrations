"""`AlgentaToolset` -- a governed-execution-aware `pydantic_ai.toolsets.wrapper.WrapperToolset`
wrapping an `MCPToolset` pointed at the caller's own self-hosted Algenta Engine.

`execute_decision` is fully synchronous: a call either succeeds (a typed `ExecutionReceipt`) or
is blocked in the same call by exactly one of three named policy gates (a 409-shaped
`ExecutionDenial`) -- there is no asynchronous "pending approval" state to model, and so no use
for pydantic-ai's deferred-tool-approval primitives (`ApprovalRequired` /
`DeferredToolRequests`) here. `AlgentaToolset.call_tool` maps a denial onto pydantic-ai's
*denial* idiom instead -- returning [`ToolDenied`][pydantic_ai.tools.ToolDenied], the same
primitive a human reviewer's "no" produces -- since a policy-gate block is exactly that: a
deliberate "no", decided synchronously by the engine rather than by a human in the loop.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic_ai.tools import AgentDepsT, RunContext, ToolDenied
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from .contract import (
    DEFAULT_PROFILE,
    NEVER_MODEL_FACING_FIELDS,
    ToolProfile,
    resolve_profile_tool_names,
)
from .receipts import ExecutionReceipt, parse_denial, parse_receipt

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
    2. **Typed `execute_decision` results**: `execute_decision`'s raw result is parsed into a
       typed [`ExecutionReceipt`][pydantic_ai_algenta.receipts.ExecutionReceipt] (success) or
       mapped to [`ToolDenied`][pydantic_ai.tools.ToolDenied] (a named policy-gate denial) --
       see `call_tool` below. Every other tool's result (`plan_decision`, `log_decision`,
       `get_contract`, ...) is freeform and passes through unchanged; this package doesn't
       invent a shared envelope for tools that don't have one.
    3. **The denial/failure mapping** (see `call_tool` below) between `execute_decision`'s real
       result shape and pydantic-ai's own `ToolReturnPart.outcome` mechanism.

    Example:

    ```python {test="skip"}
    from pydantic_ai import Agent
    from pydantic_ai_algenta import AlgentaToolset

    toolset = AlgentaToolset(base_url="http://localhost:8000/mcp", profile="observe")
    agent = Agent("openai:gpt-5", toolsets=[toolset])
    ```
    """

    profile: ToolProfile = DEFAULT_PROFILE
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt

    def __init__(
        self,
        *,
        base_url: str | None = None,
        profile: ToolProfile = DEFAULT_PROFILE,
        wrapped: AbstractToolset[AgentDepsT] | None = None,
        receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
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
            receipt_model: The `ExecutionReceipt` subclass to validate `execute_decision`'s
                successful result against. Override if your engine's receipt envelope has grown
                fields you want typed (the base model already accepts and preserves unknown
                fields via `extra="allow"`, so most callers won't need this).
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
        """Call the wrapped tool, then map `execute_decision`'s real result onto pydantic-ai's primitives.

        - A named policy-gate denial (the real, synchronous `{"error": {"code":
          "execution_blocked_<gate>", "gate": ..., "message": ..., "override_hint": ...}}`
          shape, where `<gate>` is one of `"idempotency"`, `"confidence"`, `"risk_floor"`) --
          returns [`ToolDenied`][pydantic_ai.tools.ToolDenied], which pydantic-ai turns into
          `ToolReturnPart(outcome="denied")` with the real gate name and the engine's own
          message/override_hint preserved (see `ExecutionDenial.denial_reason`).
        - A successful result that validates as an [`ExecutionReceipt`]
          [pydantic_ai_algenta.receipts.ExecutionReceipt] -- returned as-is (a typed object, not
          a raw dict), becoming `ToolReturnPart(outcome="success")`. Note that
          `execution_status == "failed"` (the downstream webhook delivery failed) is still this
          branch: the call itself succeeded, and the engine is honestly reporting delivery
          failure, not refusing the call.
        - Anything else -- a freeform result from a tool that isn't `execute_decision`
          (`plan_decision`, `log_decision`, `get_contract`'s discovery payload, ...) -- passes
          through unchanged.

        A genuine transport/HTTP-level failure (a dropped connection, a 5xx, a timeout) is never
        this method's concern: it already surfaces as whatever pydantic-ai's own `MCPToolset`
        raises out of `super().call_tool()` (typically
        [`ToolFailed`][pydantic_ai.exceptions.ToolFailed] or
        [`ModelRetry`][pydantic_ai.exceptions.ModelRetry], depending on `tool_error_behavior`)
        before this method ever sees a result to parse.

        There is no "pending approval" branch: `execute_decision` is fully synchronous, so this
        method never raises [`ApprovalRequired`][pydantic_ai.exceptions.ApprovalRequired] and no
        call through this toolset ever produces a `DeferredToolRequests`.

        `force`/`override_safety` are stripped from `tool_args` before the wrapped call, as a
        second layer behind the schema-level scrub in `get_tools` -- see
        `contracts/integration-tool-contract.json`'s `never_model_facing_note`.
        """
        scrubbed_args = {k: v for k, v in tool_args.items() if k not in NEVER_MODEL_FACING_FIELDS}
        raw_result = await super().call_tool(name, scrubbed_args, ctx, tool)

        denial = parse_denial(raw_result)
        if denial is not None:
            return ToolDenied(message=denial.denial_reason())

        receipt = parse_receipt(raw_result, model=self.receipt_model)
        if receipt is not None:
            return receipt

        return raw_result


__all__ = ["AlgentaToolset", "DEFAULT_ALGENTA_BASE_URL", "ALGENTA_BASE_URL_ENV_VAR"]
