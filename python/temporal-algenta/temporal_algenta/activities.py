"""`AlgentaActivities` -- a self-hosted Algenta Engine's MCP tool surface as Temporal
activities, with typed receipts on the success path and structured denials as typed,
non-retryable `ApplicationError`s on the blocked path.

Design notes:

- **Session-per-call.** Every activity call opens its own short-lived `AlgentaMcpClient`
  session. Temporal may retry an activity on a different worker or after a crash, so no attempt
  may depend on connection state surviving from a previous attempt; the engine's own
  idempotency gate (see `recipes/idempotent_activity_receipt_dedup.py`) is what dedups a
  retried delivery, and it only works when each attempt is a complete, self-contained call.
- **Denials are failures, not results.** A blocked `execute_decision` raises
  `ApplicationError(type=<denial code>, non_retryable=True, details=[denial body])` -- see
  `temporal_algenta.errors.denial_application_error`. Workflow code recovers the structured
  denial with `temporal_algenta.types.denial_from_activity_error`. A receipt with
  `execution_status="failed"` (the webhook target rejected delivery) is NOT a denial: the call
  succeeded, and the receipt is returned normally for the workflow to react to.
- **Profile enforcement at call time.** The configured tool profile is enforced inside every
  activity (via the client) rather than by withholding activity registration: all seven
  activities are always registered with the worker, so a profile violation surfaces as an
  explicit, typed, non-retryable refusal (`PROFILE_DENIED_ERROR_TYPE`) instead of a confusing
  "activity not registered" workflow failure.
- **`force` / `override_safety` are operator-facing here.** They arrive as explicit fields on
  `ExecuteDecisionInput`, set by workflow code (e.g. from a human approval signal) -- never
  defaulted to `True`, and never part of any model-facing schema. See
  `temporal_algenta.contract.NEVER_MODEL_FACING_FIELDS` for the contract wording this honors.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from mcp.shared.exceptions import McpError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .client import AlgentaMcpClient
from .contract import (
    DEFAULT_PROFILE,
    EXECUTE_DECISION,
    GET_CONTRACT,
    LOG_DECISION,
    PLAN_DECISION,
    QUERY_DATA,
    RECOMMEND,
    SIMULATE,
    TOOL_PROFILES,
    ToolProfile,
)
from .errors import (
    AlgentaExecutionBlocked,
    AlgentaGovernedCallError,
    AlgentaToolCallFailed,
    AlgentaToolDenied,
    denial_application_error,
)
from .receipts import ExecutionDenial, ExecutionReceipt, parse_receipt
from .types import (
    PROFILE_DENIED_ERROR_TYPE,
    SESSION_ERROR_TYPE,
    TOOL_ERROR_TYPE,
    UNEXPECTED_RESULT_ERROR_TYPE,
    ExecuteDecisionInput,
    ExecutionReceiptData,
    LogDecisionInput,
)


class AlgentaActivities:
    """The seven contract tools as Temporal activities; register `all_activities()` with a
    `Worker`.

    Args:
        base_url: The self-hosted Algenta MCP endpoint (default: `ALGENTA_BASE_URL` env var,
            then `"http://localhost:8000/mcp"`).
        profile: The tool profile every activity call is allowed under -- `"observe"`
            (default), `"govern"`, `"execute"`, or `"full"`. A workflow that only simulates and
            recommends should run its worker under `"observe"` and structurally cannot execute
            anything; executing requires an explicit operator opt-in here, matching the
            contract's "execute is never enabled by default" rule.
        headers: Extra HTTP headers for the endpoint (e.g. a static bearer token).
        read_timeout: Per-request response timeout for the underlying MCP session (the SDK's
            `read_timeout_seconds`; 60s when omitted). Bound it to make a wedged session fail
            fast as a retryable `TOOL_ERROR_TYPE` activity error that your `RetryPolicy` rides
            out on a fresh session -- see `AlgentaMcpClient`'s `read_timeout` note.
        denial_model: The `ExecutionDenial` subclass to validate denial bodies against.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        profile: ToolProfile = DEFAULT_PROFILE,
        headers: dict[str, str] | None = None,
        read_timeout: timedelta | None = None,
        denial_model: type[ExecutionDenial] = ExecutionDenial,
    ) -> None:
        if profile not in TOOL_PROFILES:
            raise ValueError(f"Unknown tool profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}.")
        self.base_url = base_url
        self.profile = profile
        self.headers = headers
        self.read_timeout = read_timeout
        self.denial_model = denial_model

    def all_activities(self) -> list[Callable[..., Any]]:
        """Every activity this class defines, as bound methods -- pass straight to
        `Worker(..., activities=algenta.all_activities())`.

        All seven are always registered regardless of `profile` (see the module docstring for
        why enforcement lives at call time instead).
        """
        return [
            self.get_contract,
            self.query_data,
            self.simulate,
            self.recommend,
            self.plan_decision,
            self.log_decision,
            self.execute_decision,
        ]

    def _client(self) -> AlgentaMcpClient:
        """A fresh, unopened client for one activity call (see the module docstring's
        session-per-call note)."""
        return AlgentaMcpClient(
            base_url=self.base_url,
            profile=self.profile,
            headers=self.headers,
            read_timeout=self.read_timeout,
            denial_model=self.denial_model,
        )

    @staticmethod
    def _map_error(error: AlgentaGovernedCallError) -> ApplicationError:
        """Translate a client-side governed-call error into the activity's Temporal failure.

        A recognized policy denial becomes a typed, non-retryable `ApplicationError` carrying
        the denial body; a profile refusal becomes a non-retryable typed refusal; anything else
        (an unclassified tool-execution error) stays retryable -- it may be transient, and the
        workflow's `RetryPolicy` is the right place to bound it.
        """
        if isinstance(error, AlgentaExecutionBlocked) and error.denial is not None:
            return denial_application_error(error.denial)
        if isinstance(error, AlgentaToolDenied):
            return ApplicationError(str(error), type=PROFILE_DENIED_ERROR_TYPE, non_retryable=True)
        if isinstance(error, AlgentaToolCallFailed):
            return ApplicationError(str(error), type=TOOL_ERROR_TYPE)
        return ApplicationError(str(error), type=TOOL_ERROR_TYPE)

    @staticmethod
    def _raise_mapped(error: Exception) -> None:
        """Raise the activity-facing form of a captured call error.

        Governed-call errors become the typed Temporal failure (`_map_error`). MCP session
        failures (`McpError` -- e.g. a read timeout on a wedged session establishment, see
        `AlgentaMcpClient`'s `read_timeout` note) become a retryable `TOOL_ERROR_TYPE`
        `ApplicationError`: bounded, typed, and safe for the workflow's `RetryPolicy` to ride
        out on a fresh session. Anything else (a raw transport error) propagates unchanged,
        which Temporal also treats as retryable by default.
        """
        if isinstance(error, AlgentaGovernedCallError):
            raise AlgentaActivities._map_error(error) from error
        if isinstance(error, McpError):
            raise ApplicationError(str(error), type=SESSION_ERROR_TYPE) from error
        raise error

    async def _call_dict_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one freeform, non-safety-critical tool and return its dict payload.

        Shared by every activity except `execute_decision`. These tools (`get_contract`,
        `query_data`, `simulate`, `recommend`, `plan_decision`, `log_decision`) carry no policy
        gates on the real engine, so there is no receipt/denial parsing here -- just the
        governed-call error mapping and a shape check that the payload is the dict these tools
        all return.
        """
        error, payload = await self._call(tool_name, arguments)
        if error is not None:
            self._raise_mapped(error)
        if not isinstance(payload, dict):
            raise ApplicationError(
                f"Algenta tool {tool_name!r} returned a non-dict payload: {type(payload).__name__}",
                type=UNEXPECTED_RESULT_ERROR_TYPE,
                non_retryable=True,
            )
        return payload

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> tuple[Exception | None, Any]:
        """Call one tool over a fresh MCP session, returning `(error, payload)`.

        The error is *captured inside* the session and re-raised by the caller only after the
        session has closed cleanly: an exception crossing the MCP client's context-manager
        boundary gets wrapped into an `ExceptionGroup` by the SDK's internal anyio task groups
        (verified against the installed `mcp` SDK), which would hide the real failure type from
        Temporal's retry machinery. Catching `Exception` (not `BaseException`) leaves
        cancellation untouched, as Temporal's activity cancellation requires.
        """
        async with self._client() as client:
            try:
                return None, await client.call_tool(tool_name, arguments)
            except Exception as error:
                return error, None

    @activity.defn
    async def get_contract(self) -> dict[str, Any]:
        """Fetch the engine's live capability/discovery contract (observability tier)."""
        return await self._call_dict_tool(GET_CONTRACT, {})

    @activity.defn
    async def query_data(self, dataset: str) -> dict[str, Any]:
        """Run one governed exact query against an authorized, connected dataset."""
        return await self._call_dict_tool(QUERY_DATA, {"dataset": dataset})

    @activity.defn
    async def simulate(self, scenario: str) -> dict[str, Any]:
        """Run a Monte Carlo / decision simulation over a scenario definition."""
        return await self._call_dict_tool(SIMULATE, {"scenario": scenario})

    @activity.defn
    async def recommend(self, scenario: str) -> dict[str, Any]:
        """Return a governed recommendation for a decision scenario."""
        return await self._call_dict_tool(RECOMMEND, {"scenario": scenario})

    @activity.defn
    async def plan_decision(self, scenario: str) -> dict[str, Any]:
        """Produce a structured decision-plan summary for review (govern tier)."""
        return await self._call_dict_tool(PLAN_DECISION, {"scenario": scenario})

    @activity.defn
    async def log_decision(self, input: LogDecisionInput) -> dict[str, Any]:
        """Persist a decision record to decision memory; returns the engine's record carrying
        the `decision_id` a later `execute_decision` call needs (govern tier)."""
        arguments: dict[str, Any] = {"chosen_action": input.chosen_action}
        if input.rationale is not None:
            arguments["rationale"] = input.rationale
        if input.note is not None:
            arguments["note"] = input.note
        return await self._call_dict_tool(LOG_DECISION, arguments)

    @activity.defn
    async def execute_decision(self, input: ExecuteDecisionInput) -> ExecutionReceiptData:
        """Dispatch one logged decision to its webhook for real-world execution (execute tier).

        Returns the engine's `ExecutionReceipt` as a wire-safe `ExecutionReceiptData`. Raises a
        typed, non-retryable `ApplicationError` carrying the structured denial when the engine
        blocks the call on a named policy gate -- see the module docstring.
        """
        arguments: dict[str, Any] = {
            "decision_id": input.decision_id,
            "webhook_url": input.webhook_url,
            "timeout_seconds": input.timeout_seconds,
        }
        # `force` / `override_safety` are forwarded only when the operator-facing input
        # explicitly sets them -- never by default, never implicitly (see
        # `temporal_algenta.contract.NEVER_MODEL_FACING_FIELDS`).
        if input.force:
            arguments["force"] = True
        if input.override_safety:
            arguments["override_safety"] = True
        if input.metadata is not None:
            arguments["metadata"] = input.metadata

        error, payload = await self._call(EXECUTE_DECISION, arguments)
        if error is not None:
            self._raise_mapped(error)

        receipt: ExecutionReceipt | None = parse_receipt(payload)
        if receipt is None:
            raise ApplicationError(
                "Algenta tool 'execute_decision' completed without error but its payload is not "
                f"an ExecutionReceipt: {payload!r}",
                type=UNEXPECTED_RESULT_ERROR_TYPE,
                non_retryable=True,
            )
        return ExecutionReceiptData(
            decision_id=receipt.decision_id,
            webhook_url=receipt.webhook_url,
            execution_status=receipt.execution_status,
            response_code=receipt.response_code,
            executed_at=receipt.executed_at,
            policy_snapshot_id=receipt.policy_snapshot_id,
            schema_snapshot_id=receipt.schema_snapshot_id,
            manifest_version=receipt.manifest_version,
            payload_summary=receipt.payload_summary,
            safety_overridden=receipt.safety_overridden,
        )


__all__ = ["AlgentaActivities"]
