"""`GovernedReceiptHook` -- the `after_tool` `Agent` hook that maps the real `execute_decision`
denial shape onto Haystack's own real hook seam.

**Corrected against the real engine contract.** An earlier version of this module additionally
built a `before_tool` `ConfirmationHook` pre-call gate (via `default_confirmation_hook` /
`AlgentaGovernanceHooks.before_tool`) that asked a human to confirm the model's *request* to call
`execute_decision`, separately from `GovernedReceiptHook` checking whether the engine's own
out-of-band plan approval had actually landed (`approval_state == "pending"`). That whole design
existed to bridge two different notions of "waiting for a human" across a fictional async approval
boundary that doesn't exist on the real tool: `execute_decision` never comes back pending -- it is
a 200 success or a same-call 409 denial, full stop. There is nothing to ask a human to confirm
*before* the call that the call's own result doesn't already tell you *after* the call, so the
pre-call `ConfirmationHook` wiring is removed outright rather than kept for a state that cannot
occur. (Haystack's `ConfirmationHook` itself is a perfectly real, general-purpose primitive --
nothing here says otherwise -- it just has no job to do in *this* package once the fictional
approval-state bridge it was built for is gone; a caller who wants a human-in-the-loop gate before
letting a model call `execute_decision` at all can still wire Haystack's own `ConfirmationHook`
directly, the same way they would for any other tool.)

What's left, and is the one piece of real safety enforcement this package provides: **this
module's own `after_tool` hook**, `GovernedReceiptHook`. It parses the real outcome out of the
tool-result message(s) `Agent._run_step` just wrote into `state.data["messages"]`
(`haystack_algenta.receipts.extract_execution_outcome_from_tool_result`) and raises
`AlgentaToolDenied` -- naming the real gate (`"idempotency"` / `"confidence"` / `"risk_floor"`) --
for the real 409 denial shape. A successful `ExecutionReceipt` (even one whose
`execution_status == "failed"` -- a webhook-delivery outcome, not a policy verdict) and any
non-`execute_decision` tool's result are both left alone.

Verified live (see the package README's "Approval mapping" section): this exception propagates out
of `agent.run()` **completely unmodified** -- no `ToolInvocationError` wrapping, no swallowing --
because `Agent._run_step`'s `_run_hooks(self.hooks, AFTER_TOOL, state)` call has no surrounding
`try`/`except` anywhere in `agent.py` (grep-verified against the installed package). This is the
one seam in Haystack's `Agent` loop that behaves the way raising `MiddlewareFailure` directly from
a tool body does in MAF, or the way an uncaught exception from a LangChain tool coroutine does --
and it is genuinely the *only* one: raising from inside a wrapped `Tool`'s own `function` instead
(what `haystack_algenta.toolset` deliberately does not do) gets unconditionally rewrapped into
`ToolInvocationError`, and with a real `Agent`'s documented default
`raise_on_tool_invocation_failure=False`, silently swallowed into an ordinary tool-result message
the model then sees as plain text -- never raised out of `agent.run()` at all.

**A `Pipeline`, or any caller invoking `Tool.invoke()`/`invoke_async()` directly instead of through
an `Agent`, gets no hook at all** -- hooks are an `Agent`-loop concept; nothing here runs for a bare
tool call. For that caller, `haystack_algenta.receipts.extract_execution_outcome_from_tool_result`
is the same parsing this module's hook uses, exposed directly so a `Pipeline`/direct-`invoke()`
caller can call it themselves on whatever `Tool.invoke()` returned and decide what to do -- an
honest, undisguised capability, not a fabricated approval-pause mechanism.
"""

from __future__ import annotations

from haystack.components.agents.state.state import State

from .exceptions import AlgentaToolDenied
from .receipts import ExecutionBlocked, ExecutionReceipt, extract_execution_outcome_from_tool_result


class GovernedReceiptHook:
    """A real Haystack `after_tool` `Agent` hook: parses every just-produced tool-result message
    in `state` and raises `AlgentaToolDenied` for the first real `execute_decision` 409 denial it
    finds.

    Detects the denial by shape, not by tool name -- `extract_execution_outcome_from_tool_result`
    returning an `ExecutionBlocked` -- so any other tool's result (which never validates as either
    `ExecutionReceipt` or `ExecutionBlocked`) is silently skipped, and a *successful*
    `execute_decision` `ExecutionReceipt` is silently skipped too (nothing to raise for; a
    `"failed"` `execution_status` is a webhook-delivery outcome, not a governance denial).

    Only makes sense at the `after_tool` hook point, where tool-result messages actually exist in
    `state` -- `allowed_hook_points` restricts it there, and Haystack's own `Agent` construction
    enforces that restriction (see `haystack.components.agents.agent._validate_hooks`).
    """

    allowed_hook_points = ("after_tool",)

    def __init__(
        self,
        *,
        receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
        blocked_model: type[ExecutionBlocked] = ExecutionBlocked,
    ) -> None:
        self.receipt_model = receipt_model
        self.blocked_model = blocked_model

    def run(self, state: State) -> None:
        for message in state.data.get("messages") or []:
            result = getattr(message, "tool_call_result", None)
            if result is None:
                continue
            outcome = extract_execution_outcome_from_tool_result(
                result.result, receipt_model=self.receipt_model, blocked_model=self.blocked_model
            )
            if not isinstance(outcome, ExecutionBlocked):
                continue
            tool_name = result.origin.tool_name
            hint = f" ({outcome.override_hint})" if outcome.override_hint else ""
            raise AlgentaToolDenied(
                f"Algenta tool {tool_name!r} was blocked by the real {outcome.gate!r} policy gate "
                f"({outcome.code}): {outcome.message}{hint}",
                blocked=outcome,
            )

    async def run_async(self, state: State) -> None:
        # Pure, synchronous, in-memory message parsing -- no I/O, so there is nothing genuinely
        # async to do here. Defined anyway so `_run_hooks_async` doesn't have to offload this hook
        # to a worker thread via `_execute_component_async`'s sync fallback.
        self.run(state)


def build_algenta_governance_hooks(
    *,
    receipt_model: type[ExecutionReceipt] = ExecutionReceipt,
    blocked_model: type[ExecutionBlocked] = ExecutionBlocked,
) -> dict[str, list[GovernedReceiptHook]]:
    """Build the `dict[HookPoint, list[Hook]]` shape `Agent(hooks=...)` expects, registering the
    one real piece of safety enforcement this package provides.

    ```python
    from haystack.components.agents import Agent
    from haystack_algenta import build_algenta_governance_hooks, create_algenta_tools

    toolset = create_algenta_tools(profile="execute")
    agent = Agent(chat_generator=..., tools=toolset, hooks=build_algenta_governance_hooks())
    ```

    There is no `before_tool` entry: unlike the fictional design this package used to have, there
    is no pre-call gate left for this package to wire on `execute_decision`'s behalf (see this
    module's docstring). Wire Haystack's own `ConfirmationHook` directly, independently of this
    function, if you want a human-in-the-loop pre-call gate for your own reasons.
    """
    return {"after_tool": [GovernedReceiptHook(receipt_model=receipt_model, blocked_model=blocked_model)]}


__all__ = [
    "GovernedReceiptHook",
    "build_algenta_governance_hooks",
]
