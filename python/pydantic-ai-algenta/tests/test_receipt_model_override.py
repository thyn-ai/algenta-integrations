"""`AlgentaToolset(receipt_model=...)` must actually be the model `call_tool` validates
against -- not just an accepted-but-ignored constructor argument.
"""

from __future__ import annotations

import pytest
from pydantic_ai.toolsets.function import FunctionToolset
from pydantic_ai_algenta import AlgentaToolset, ExecutionReceipt

from .helpers import bare_run_context


class ReceiptWithShout(ExecutionReceipt):
    def shout_status(self) -> str:
        return self.execution_status.upper()


def execute_decision(decision_id: str, webhook_url: str) -> dict:
    return {"decision_id": decision_id, "webhook_url": webhook_url, "execution_status": "delivered"}


@pytest.mark.anyio
async def test_call_tool_validates_against_the_custom_receipt_model() -> None:
    toolset = AlgentaToolset(
        wrapped=FunctionToolset([execute_decision]), profile="execute", receipt_model=ReceiptWithShout
    )
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)

    result = await toolset.call_tool(
        "execute_decision", {"decision_id": "dec-1", "webhook_url": "https://example.com/hook"}, ctx, tools["execute_decision"]
    )

    assert isinstance(result, ReceiptWithShout)
    assert result.shout_status() == "DELIVERED"


@pytest.mark.anyio
async def test_default_receipt_model_is_the_base_class() -> None:
    toolset = AlgentaToolset(wrapped=FunctionToolset([execute_decision]), profile="execute")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)

    result = await toolset.call_tool(
        "execute_decision", {"decision_id": "dec-1", "webhook_url": "https://example.com/hook"}, ctx, tools["execute_decision"]
    )

    assert type(result) is ExecutionReceipt
