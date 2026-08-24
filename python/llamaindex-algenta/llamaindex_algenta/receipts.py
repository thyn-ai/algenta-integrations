"""`ExecutionReceipt` / `ExecutionDenial` -- the real typed shapes a governed Algenta
`execute_decision` MCP tool call returns, plus the plumbing that recovers them from what a real
`mcp.types.CallToolResult` actually looks like.

**Only `execute_decision` gets this treatment.** Verified directly against the real engine's MCP
tool surface (`apps/mcp_server/tools/decisions.py`, private, but its facts are authoritative
here): `plan_decision` is a freeform passthrough to `POST /v1/decisions/plan`, `log_decision`
returns its own small `{decision_id, chosen_action, expected_value, confidence, created_at,
note}` shape, and `query_data`/`simulate`/`recommend`/`get_contract` each return whatever payload
they document -- none of that is safety-critical and none of it is gated. `execute_decision`
alone is safety-critical: it either succeeds with a real `ExecutionReceipt` (below) or is blocked
*synchronously* by exactly one of three named policy gates, surfaced as a real 409 whose body is
an `ExecutionDenial` (below). `llamaindex_algenta.toolset` therefore only ever tries to parse
these two shapes for the `execute_decision` tool call specifically -- every other tool's result is
passed through to the model unchanged, exactly as it came back from the engine.

This module was previously built against a fictional, generic `GovernedExecutionReceipt` envelope
(`status`/`code`/`approval_state`/`plan_hash`/... on *every* governed tool, with an asynchronous
"pending approval" state) -- that shape does not exist anywhere in the real engine. It has been
replaced with the two real shapes above. See the package README's "Execution outcome mapping"
section for the full accounting.
"""

from __future__ import annotations

import json
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

#: The three, and only three, real named gates `execute_decision` can synchronously block on
#: (verified against the real engine: a 409 with `{"error": {"code": "execution_blocked_<gate>",
#: "gate": "<gate>", ...}}`, `<gate>` always one of these three literal strings).
#:
#: - `"idempotency"`: this decision has already been delivered; `force=true` bypasses this gate
#:   for one re-execution only.
#: - `"confidence"`: the decision's confidence is below `policy.min_confidence`; bypassable only
#:   via `override_safety=true`.
#: - `"risk_floor"`: `risk_p5` is below `-policy.risk_floor`; bypassable only via
#:   `override_safety=true`.
ExecutionGate = Literal["idempotency", "confidence", "risk_floor"]

NAMED_EXECUTION_GATES: Final[frozenset[str]] = frozenset({"idempotency", "confidence", "risk_floor"})


class ExecutionReceipt(BaseModel):
    """The real, successful (HTTP 200) result of an `execute_decision` MCP tool call.

    `extra="allow"` on purpose: the engine may add fields to this envelope over time, and a newer
    engine talking to an older version of this package should not fail to parse just because it
    sent one more field than this model knew about when it was released.
    """

    model_config = ConfigDict(extra="allow")

    decision_id: str
    webhook_url: str
    execution_status: Literal["delivered", "failed"]
    response_code: int | None = None
    executed_at: str | None = None
    policy_snapshot_id: str | None = None
    schema_snapshot_id: str | None = None
    manifest_version: str | None = None
    payload_summary: Any = None
    safety_overridden: bool = False


class ExecutionDenial(BaseModel):
    """The real, synchronous HTTP 409 body an `execute_decision` call gets back when one of the
    three named gates (`NAMED_EXECUTION_GATES`) blocks it -- the JSON under the response's
    top-level `"error"` key, e.g. `{"code": "execution_blocked_confidence", "gate": "confidence",
    "message": "...", "override_hint": "..."}`.
    """

    model_config = ConfigDict(extra="allow")

    code: str
    gate: ExecutionGate
    message: str
    override_hint: str | None = None


def parse_execution_receipt(
    raw_result: Any, *, model: type[ExecutionReceipt] = ExecutionReceipt
) -> ExecutionReceipt | None:
    """Parse an already-unwrapped `execute_decision` result payload into an `ExecutionReceipt`.

    Returns `None` (rather than raising) when `raw_result` doesn't validate as one -- in
    particular, this also correctly rejects a denial payload (no `decision_id`/`webhook_url`/
    `execution_status` on that shape).
    """
    if not isinstance(raw_result, dict):
        return None
    try:
        return model.model_validate(raw_result)
    except ValidationError:
        return None


def parse_execution_denial(
    raw_result: Any, *, model: type[ExecutionDenial] = ExecutionDenial
) -> ExecutionDenial | None:
    """Parse an already-unwrapped `execute_decision` result payload into an `ExecutionDenial`,
    when it's shaped like the real `{"error": {"code": ..., "gate": ..., ...}}` 409 body.

    Returns `None` for anything else -- including a well-formed `ExecutionReceipt`, or a
    dict whose `"error"` sub-object doesn't carry one of `NAMED_EXECUTION_GATES` in `gate`
    (`pydantic`'s `Literal` validation rejects any other value).
    """
    if not isinstance(raw_result, dict):
        return None
    error = raw_result.get("error")
    if not isinstance(error, dict):
        return None
    try:
        return model.model_validate(error)
    except ValidationError:
        return None


def is_call_error(raw: Any) -> bool:
    """Whether `raw` is a real `CallToolResult`/duck-typed equivalent reporting `isError=True`.

    This is the MCP *protocol*-level failure case: something raised inside the connected server's
    own tool implementation for a reason that is *not* one of the three named execution gates
    above (those come back as an ordinary, well-formed denial body, not a protocol-level error),
    caught by the MCP SDK, and returned as ordinary (non-raising) response data -- e.g.
    `content=[TextContent(text="Error executing tool blow_up: ...")]`.
    """
    return bool(getattr(raw, "isError", False))


def call_error_text(raw: Any) -> str:
    """Best-effort human-readable text for an `is_call_error(raw)` result.

    Real MCP servers put the exception message in a `TextContent` block (e.g. `"Error executing
    tool blow_up: ..."`) -- this joins every such block, falling back to `str(raw)` if none are
    found (a duck-typed test double, for instance).
    """
    content = getattr(raw, "content", None) or []
    texts = [item.text for item in content if getattr(item, "type", None) == "text" and hasattr(item, "text")]
    return "; ".join(texts) if texts else str(raw)


def unwrap_call_tool_result(raw: Any) -> Any:
    """Unwrap a real `mcp.types.CallToolResult` (or a duck-typed/plain-dict equivalent, for tests
    and escape hatches that don't go through a real MCP wire) into the tool's actual payload.

    Preference order, verified live against a real `fastmcp` stub server round trip:

    1. `raw.structuredContent` when it's already a non-empty dict -- the real `CallToolResult`
       field the MCP spec reserves for exactly this, with no second JSON-string layer to peel.
    2. `raw.content`'s first `TextContent`-shaped block (`type == "text"`), `json.loads()`-ed --
       the fallback for a server that only populates `content`, not `structuredContent`.
    3. `raw` itself, if it's already a plain `dict` (the shape a fake/test `ClientSession` or a
       future non-MCP escape hatch might hand back directly, with no `CallToolResult` wrapping
       at all).
    4. `raw` itself, if it's a JSON string (defensive -- no real code path in this package
       produces this, since `BasicMCPClient.call_tool` always returns a typed object, but kept
       for symmetry with the sibling packages that do unwrap a JSON-string layer).

    Returns `None` when nothing recognizable is found -- the deliberate signal callers use to
    treat a result as a non-governed passthrough, or when `raw` genuinely isn't parseable (e.g. a
    non-JSON string).
    """
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    content = getattr(raw, "content", None)
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if getattr(item, "type", None) == "text" and isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue
        return None

    if isinstance(raw, dict):
        # Support a plain dict that already carries the MCP `content`/`structuredContent` shape
        # (e.g. a hand-built fake `CallToolResult`-like dict in a test), as well as a plain
        # already-unwrapped payload dict handed back by a non-MCP escape hatch.
        if isinstance(raw.get("structuredContent"), dict) and raw["structuredContent"]:
            return raw["structuredContent"]
        if isinstance(raw.get("content"), list):
            for item in raw["content"]:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if item.get("type") == "text" and isinstance(text, str):
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        continue
            return None
        return raw

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    return None


__all__ = [
    "NAMED_EXECUTION_GATES",
    "ExecutionDenial",
    "ExecutionGate",
    "ExecutionReceipt",
    "call_error_text",
    "is_call_error",
    "parse_execution_denial",
    "parse_execution_receipt",
    "unwrap_call_tool_result",
]
