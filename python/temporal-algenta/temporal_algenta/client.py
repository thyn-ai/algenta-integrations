"""`AlgentaMcpClient` -- a minimal, async MCP client for a self-hosted Algenta Engine's tool
surface, used by `temporal_algenta.activities.AlgentaActivities`.

Deliberately thin: it speaks streamable-HTTP MCP via the base `mcp` SDK (no framework
adapters), enforces the active tool profile at call time (defense-in-depth -- see
`temporal_algenta.contract`), unwraps the `CallToolResult` envelope into its payload, and maps
the three real policy-gate denials onto `AlgentaExecutionBlocked`. Everything else -- Temporal
failure typing, retry semantics -- is the activity layer's job
(`temporal_algenta.activities`), keeping this client usable and testable on its own.

One client = one MCP session = one `async with` block. The activity layer opens a fresh client
per activity call on purpose: Temporal may retry an activity on a different worker or after a
crash, and a session-per-call keeps every attempt self-contained and idempotency-safe instead
of depending on shared connection state surviving retries. The cost is one `initialize`
handshake per call, negligible next to the governed call itself.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from types import TracebackType
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult

from .contract import DEFAULT_PROFILE, ToolProfile, is_tool_allowed_for_profile
from .errors import AlgentaExecutionBlocked, AlgentaToolCallFailed, AlgentaToolDenied
from .receipts import ExecutionDenial, parse_denial

#: Default self-hosted Algenta MCP endpoint. Matches this whole repository's standing rule:
#: every default points at the caller's own self-hosted deployment, never a hosted-by-Algenta
#: cloud endpoint. Override via `ALGENTA_BASE_URL` or the `base_url=` constructor argument.
DEFAULT_ALGENTA_BASE_URL = "http://localhost:8000/mcp"

ALGENTA_BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"


def extract_call_tool_payload(call_result: CallToolResult) -> Any:
    """Unwrap the actual payload out of a raw MCP `CallToolResult` envelope.

    Prefers `structuredContent` (the modern, `outputSchema`-driven shape), falls back to parsing
    the first text content block as JSON (the shape an MCP server without a declared output
    schema uses -- which is exactly what `tests/stub_server.py`'s plain-`dict`-returning tools
    produce). When that text block isn't valid JSON on its own -- e.g. an `isError=True` result
    whose text some framework layer wrapped in its own prose around the real JSON body, verified
    directly against the installed `mcp` SDK's `mcp.server.fastmcp.tools.base.Tool.run` -- the
    raw text is returned as-is rather than `None`, so `receipts.parse_denial`'s own
    embedded-JSON recovery still gets a chance at it. Returns `None` only when there's truly no
    content to inspect. Mirrors `langchain_algenta.interceptor.extract_call_tool_payload` and
    `typescript/algenta-tools`'s `extractToolPayload`.
    """
    if call_result.structuredContent is not None:
        return call_result.structuredContent
    for block in call_result.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


#: How long one session-establishment attempt may take before it is aborted and retried (see
#: `__aenter__`). Transport setup has no observable side effects, so retrying it is
#: semantics-free.
ESTABLISH_TIMEOUT = timedelta(seconds=15)
ESTABLISH_ATTEMPTS = 3


class AlgentaMcpClient:
    """One MCP session against a self-hosted Algenta Engine; an async context manager.

    Args:
        base_url: The self-hosted Algenta MCP endpoint. Defaults to the `ALGENTA_BASE_URL`
            environment variable, falling back to `"http://localhost:8000/mcp"`.
        profile: Which tool profile calls are allowed under -- one of `"observe"` (default),
            `"govern"`, `"execute"`, or `"full"`. Enforced at call time: a call to a tool
            outside the profile raises `AlgentaToolDenied` *before any network traffic*.
        headers: Extra HTTP headers for the endpoint (e.g. a static bearer token for the
            engine's own auth), forwarded to the streamable-HTTP transport.
        read_timeout: How long any single request (initialize or tool call) may wait for its
            response before raising `mcp.shared.exceptions.McpError` -- the MCP SDK's
            `read_timeout_seconds` (its own default is 60s when omitted). This bounds a wedged
            session-establishment race observed intermittently in the SDK's streamable-HTTP
            transport: a bounded failure here surfaces as a *retryable* activity error, which
            Temporal's retry policy then rides out on a fresh session -- see
            `temporal_algenta.activities`.
        denial_model: The `ExecutionDenial` subclass to validate a blocked call's
            `{"error": {...}}` body against.
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
        self.base_url = base_url or os.environ.get(ALGENTA_BASE_URL_ENV_VAR) or DEFAULT_ALGENTA_BASE_URL
        self.profile = profile
        self.headers = headers
        self.read_timeout = read_timeout
        self.denial_model = denial_model
        self._http_cm: Any = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> AlgentaMcpClient:
        """Open the session, retrying a stalled establishment a bounded number of times.

        The MCP SDK's streamable-HTTP transport intermittently deadlocks *during setup* (no
        request is ever issued and no read timeout can fire, so without a bound here the
        caller hangs forever -- reproduced directly). Each attempt is therefore wrapped in a
        hard timeout and fully abandoned before the next one; establishment has no observable
        side effects, so this retry changes no semantics.
        """
        last_error: BaseException | None = None
        for _attempt in range(ESTABLISH_ATTEMPTS):
            try:
                await asyncio.wait_for(self._open(), timeout=ESTABLISH_TIMEOUT.total_seconds())
                return self
            except BaseException as error:  # noqa: BLE001 - aborted attempt; any failure shape is retryable here
                last_error = error
                await self._abort_open()
        assert last_error is not None
        raise last_error

    async def _open(self) -> None:
        self._http_cm = streamablehttp_client(self.base_url, headers=self.headers)
        read_stream, write_stream, _ = await self._http_cm.__aenter__()
        self._session = ClientSession(read_stream, write_stream, read_timeout_seconds=self.read_timeout)
        await self._session.__aenter__()
        await self._session.initialize()

    async def _abort_open(self) -> None:
        """Best-effort teardown of a half-opened session after an aborted establishment
        attempt. Swallows every failure shape -- the half-open machinery is by definition in
        an unknown state (including the SDK's cross-task cancel-scope noise), and the abort
        must never mask the original establishment error."""
        try:
            await self._close(None, None, None)
        except BaseException:
            self._session = None
            self._http_cm = None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            # An exception from the caller's body is crossing this context manager. Close the
            # session machinery best-effort and let THAT exception propagate unchanged: the
            # MCP SDK's internal anyio task groups re-raise a crossing exception wrapped in an
            # ExceptionGroup (and can emit a spurious "cancel scope in a different task"
            # RuntimeError on top), which would hide the real error type from callers and from
            # Temporal's failure typing (verified against the installed `mcp` SDK).
            try:
                await self._close(exc_type, exc_val, exc_tb)
            except Exception:
                pass
            return None
        await self._close(exc_type, exc_val, exc_tb)
        return None

    async def _close(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.__aexit__(exc_type, exc_val, exc_tb)
            self._session = None
        if self._http_cm is not None:
            await self._http_cm.__aexit__(exc_type, exc_val, exc_tb)
            self._http_cm = None

    async def list_tool_names(self) -> frozenset[str]:
        """The names of every tool the connected engine currently advertises.

        Profile-filtered per the shared contract: what this client will actually allow a call
        to is `resolve_profile_tool_names(profile, available_tool_names=await
        client.list_tool_names())` -- see `temporal_algenta.contract`.
        """
        session = self._require_session()
        result = await session.list_tools()
        return frozenset(tool.name for tool in result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one MCP tool and return its unwrapped payload.

        Raises:
            AlgentaToolDenied: `name` is outside this client's configured profile. Raised
                before any network call -- the profile boundary is enforced client-side as
                defense-in-depth, exactly like the sibling packages' interceptors.
            AlgentaExecutionBlocked: the call was blocked synchronously on one of the three
                real named policy gates (`.denial` carries the parsed `ExecutionDenial`).
            AlgentaToolCallFailed: the call returned `isError=True` but its payload is not a
                recognized policy denial -- an unclassified tool-execution failure.
        """
        session = self._require_session()
        if not is_tool_allowed_for_profile(name, self.profile):
            raise AlgentaToolDenied(
                f"Algenta tool {name!r} is not part of the {self.profile!r} profile; refusing to call it."
            )
        result = await session.call_tool(name, arguments or {})
        payload = extract_call_tool_payload(result)
        if not result.isError:
            return payload
        denial = parse_denial(payload, model=self.denial_model)
        if denial is not None:
            raise AlgentaExecutionBlocked(
                f"Algenta tool {name!r} was blocked by the {denial.gate!r} policy gate -- {denial.message}",
                denial=denial,
            )
        raise AlgentaToolCallFailed(
            f"Algenta tool {name!r} reported an execution error: {payload!r}", payload=payload
        )

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("AlgentaMcpClient must be used as an async context manager (`async with ...`).")
        return self._session


__all__ = [
    "ALGENTA_BASE_URL_ENV_VAR",
    "DEFAULT_ALGENTA_BASE_URL",
    "AlgentaMcpClient",
    "extract_call_tool_payload",
]
