"""`AlgentaGovernanceHooks` -- the `before_tool` + `after_tool` `Agent` hook pair that maps the
governed-execution receipt contract onto Haystack's own real hook/human-in-the-loop primitives.

Verified live against installed `haystack-ai` 3.0.0 (see the package README's "Approval mapping"
section for the full reproduction): Haystack has no single tool-calling seam that can both gate a
pending call *and* see the receipt that call eventually produces (unlike `maf_algenta`'s
`_wrap_mcp_function`, which does both inside one wrapped `FunctionTool`). It has two separate,
real primitives that this module wires together instead:

1. **`haystack.hooks.human_in_the_loop.ConfirmationHook`** (a real `before_tool` hook) + a
   `BlockingConfirmationStrategy` -- a genuine pre-call gate. Proven live: with
   `AlwaysAskPolicy()` and a UI that rejects, the underlying MCP server is never contacted at all
   (no request logged). This is the same shape as MAF's `approval_mode="always_require"` and the
   TypeScript sibling's `needsApproval` -- `default_confirmation_hook` below is a thin,
   opinionated factory over it, defaulting to gating `execute_decision` specifically.

2. **`GovernedReceiptHook`** (this module's own `after_tool` hook): parses the governed-execution
   receipt out of the tool-result message(s) `Agent._run_step` just wrote into `state.data["messages"]`,
   and raises `AlgentaToolDenied` / `AlgentaApprovalStillPending` / `AlgentaToolExecutionFailed`
   for anything that isn't a clean success. Proven live that this exception propagates out of
   `agent.run()` **completely unmodified** -- no `ToolInvocationError` wrapping, no swallowing --
   because `Agent._run_step`'s `_run_hooks(self.hooks, AFTER_TOOL, state)` call has no surrounding
   `try`/`except` anywhere in `agent.py` (grep-verified against the installed package). This is
   the one seam in Haystack's `Agent` loop that behaves the way raising `MiddlewareFailure`
   directly from a tool body does in MAF, or the way an uncaught exception from a LangChain tool
   coroutine does -- and it is genuinely the *only* one: raising from inside a wrapped `Tool`'s own
   `function` instead (what `haystack_algenta.toolset` deliberately does not do) gets unconditionally
   rewrapped into `ToolInvocationError`, and with a real `Agent`'s documented default
   `raise_on_tool_invocation_failure=False`, silently swallowed into an ordinary tool-result
   message the model then sees as plain text -- never raised out of `agent.run()` at all.

**Why these two don't collapse into one concern, same as every sibling package that has both a
pre-call gate and an out-of-band engine approval:** confirming the *call* (via `ConfirmationHook`)
is a decision about whether the model may attempt `execute_decision` at all. It says nothing about
whether the connected engine's own out-of-band policy approval for that call's `plan_hash` has
actually been recorded -- proven live, the receipt can still legitimately come back
`approval_state="pending"` after a human has already approved the call itself through Haystack's
gate. `GovernedReceiptHook` is what catches that, and it does so for every governed tool, not just
`execute_decision` -- it detects a governed-execution receipt by *shape*
(`haystack_algenta.receipts.extract_receipt_from_tool_result` returning non-`None`), never by tool
name, exactly matching the contract's own "a result missing status/code should pass through
unchanged" wording.

**Registering the pre-call gate is optional; `GovernedReceiptHook` is the one piece of real safety
enforcement here and is always included by `build_algenta_governance_hooks`.** An `observe`- or
`govern`-profile agent has no `execute_decision` tool to gate in the first place, so
`build_algenta_governance_hooks(confirmation_ui=None)` (the default) registers only the
`after_tool` hook. Pass a real `confirmation_ui` (e.g. `haystack.hooks.human_in_the_loop.SimpleConsoleUI`
or `RichConsoleUI`, or your own) to also get the pre-call gate for `execute`-profile agents.

**A `Pipeline`, or any caller invoking `Tool.invoke()`/`invoke_async()` directly instead of through
an `Agent`, gets neither hook** -- hooks are an `Agent`-loop concept; nothing here runs for a bare
tool call. For that caller, `haystack_algenta.receipts.extract_receipt_from_tool_result` is the
same parsing this module's hook uses, exposed directly so a `Pipeline`/direct-`invoke()` caller can
call it themselves on whatever `Tool.invoke()` returned and decide what to do -- an honest,
undisguised capability, not a fabricated approval-pause mechanism (this package makes no such
claim, the same honest stance `litellm_algenta` documents for its own no-pause gateway path).
"""

from __future__ import annotations

from typing import Any

from haystack.components.agents.state.state import State
from haystack.hooks.human_in_the_loop import AlwaysAskPolicy, BlockingConfirmationStrategy, ConfirmationHook

from .contract import EXECUTE_DECISION
from .exceptions import AlgentaApprovalStillPending, AlgentaToolDenied, AlgentaToolExecutionFailed
from .receipts import GovernedExecutionReceipt, extract_receipt_from_tool_result


def _raise_if_not_success(*, tool_name: str, receipt: GovernedExecutionReceipt) -> None:
    if receipt.is_pending_approval():
        raise AlgentaApprovalStillPending(
            f"Algenta tool {tool_name!r} is still pending server-side policy approval "
            f"(plan_hash={receipt.plan_hash!r}) -- even if a before_tool confirmation gate already "
            "let the model's request through, that is orthogonal to the engine's own out-of-band "
            "plan approval (see the package README). Record the real approval against this "
            "plan_hash out of band, then retry from a fresh agent.run().",
            receipt=receipt,
        )
    if receipt.is_denied():
        raise AlgentaToolDenied(
            f"Algenta tool {tool_name!r} was denied by policy -- {receipt.denial_reason()}",
            receipt=receipt,
        )
    if not receipt.is_success():
        raise AlgentaToolExecutionFailed(
            f"{receipt.code}: Algenta tool {tool_name!r} did not complete successfully "
            f"(status={receipt.status!r}).",
            receipt=receipt,
        )


class GovernedReceiptHook:
    """A real Haystack `after_tool` `Agent` hook: parses every just-produced tool-result message
    in `state` and raises `AlgentaToolDenied` / `AlgentaApprovalStillPending` /
    `AlgentaToolExecutionFailed` for the first one that isn't a clean governed-execution success.

    Detects a governed-execution receipt by shape, not by tool name (see this module's docstring)
    -- `get_contract`'s discovery payload, or any other non-governed tool's result, simply doesn't
    parse as a `GovernedExecutionReceipt` and is silently skipped.

    Only makes sense at the `after_tool` hook point, where tool-result messages actually exist in
    `state` -- `allowed_hook_points` restricts it there, and Haystack's own `Agent` construction
    enforces that restriction (see `haystack.components.agents.agent._validate_hooks`).
    """

    allowed_hook_points = ("after_tool",)

    def __init__(self, *, receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt) -> None:
        self.receipt_model = receipt_model

    def run(self, state: State) -> None:
        for message in state.data.get("messages") or []:
            result = getattr(message, "tool_call_result", None)
            if result is None:
                continue
            receipt = extract_receipt_from_tool_result(result.result, model=self.receipt_model)
            if receipt is None:
                continue
            _raise_if_not_success(tool_name=result.origin.tool_name, receipt=receipt)

    async def run_async(self, state: State) -> None:
        # Pure, synchronous, in-memory message parsing -- no I/O, so there is nothing genuinely
        # async to do here. Defined anyway so `_run_hooks_async` doesn't have to offload this hook
        # to a worker thread via `_execute_component_async`'s sync fallback.
        self.run(state)


def default_confirmation_hook(
    *,
    confirmation_ui: Any,
    require_approval_for: frozenset[str] | tuple[str, ...] = (EXECUTE_DECISION,),
    confirmation_policy: Any | None = None,
) -> ConfirmationHook:
    """Build a `before_tool` `ConfirmationHook` gating `require_approval_for` (default:
    `execute_decision` only) with `confirmation_policy` (default: `AlwaysAskPolicy()`, matching
    this whole contract's "execute MUST NOT be enabled by default" stance -- opting into the gate
    at all is itself the opt-in) and `confirmation_ui`.

    `confirmation_ui` is required (no default): `BlockingConfirmationStrategy` needs a real
    `ConfirmationUI` (e.g. `haystack.hooks.human_in_the_loop.SimpleConsoleUI`/`RichConsoleUI`, or
    your own implementation of that protocol -- see Haystack's own docs) to actually ask anyone
    anything. There is no sensible silent default for "how do you ask a human" that this package
    could pick on your behalf.
    """
    policy = confirmation_policy if confirmation_policy is not None else AlwaysAskPolicy()
    names = tuple(require_approval_for)
    key: str | tuple[str, ...] = names[0] if len(names) == 1 else names
    return ConfirmationHook(
        confirmation_strategies={
            key: BlockingConfirmationStrategy(confirmation_policy=policy, confirmation_ui=confirmation_ui)
        }
    )


class AlgentaGovernanceHooks:
    """The recommended `before_tool` + `after_tool` hook pair for an Algenta-governed `Agent`.

    Pass `.as_agent_hooks()` straight to `Agent(hooks=...)`:

    ```python
    from haystack.components.agents import Agent
    from haystack.hooks.human_in_the_loop import SimpleConsoleUI
    from haystack_algenta import build_algenta_governance_hooks, create_algenta_tools

    toolset = create_algenta_tools(profile="execute")
    governance = build_algenta_governance_hooks(confirmation_ui=SimpleConsoleUI())
    agent = Agent(chat_generator=..., tools=toolset, hooks=governance.as_agent_hooks())
    ```
    """

    def __init__(self, *, before_tool: list[Any] | None = None, after_tool: list[Any] | None = None) -> None:
        self.before_tool = list(before_tool) if before_tool else []
        self.after_tool = list(after_tool) if after_tool else []

    def as_agent_hooks(self) -> dict[str, list[Any]]:
        """The `dict[HookPoint, list[Hook]]` shape `Agent(hooks=...)` expects."""
        hooks: dict[str, list[Any]] = {}
        if self.before_tool:
            hooks["before_tool"] = list(self.before_tool)
        if self.after_tool:
            hooks["after_tool"] = list(self.after_tool)
        return hooks


def build_algenta_governance_hooks(
    *,
    confirmation_ui: Any | None = None,
    require_approval_for: frozenset[str] | tuple[str, ...] = (EXECUTE_DECISION,),
    confirmation_policy: Any | None = None,
    receipt_model: type[GovernedExecutionReceipt] = GovernedExecutionReceipt,
) -> AlgentaGovernanceHooks:
    """Build the recommended hook pair.

    `after_tool` always gets a `GovernedReceiptHook` -- this is the one piece of real safety
    enforcement here, and it's always worth registering regardless of profile.

    `before_tool` only gets a `default_confirmation_hook` (gating `require_approval_for`, default
    `execute_decision`) when `confirmation_ui` is given. Leave it `None` (the default) for an
    `observe`/`govern`-profile agent that has no `execute_decision` tool to gate in the first
    place, or if you'd rather wire your own `ConfirmationHook`/UI directly alongside this pair.
    """
    before_tool = []
    if confirmation_ui is not None:
        before_tool.append(
            default_confirmation_hook(
                confirmation_ui=confirmation_ui,
                require_approval_for=require_approval_for,
                confirmation_policy=confirmation_policy,
            )
        )
    after_tool = [GovernedReceiptHook(receipt_model=receipt_model)]
    return AlgentaGovernanceHooks(before_tool=before_tool, after_tool=after_tool)


__all__ = [
    "AlgentaGovernanceHooks",
    "GovernedReceiptHook",
    "build_algenta_governance_hooks",
    "default_confirmation_hook",
]
