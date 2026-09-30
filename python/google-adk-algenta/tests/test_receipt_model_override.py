"""`AlgentaToolset(receipt_model=...)` must actually be the model `run_async` validates
against -- not just an accepted-but-ignored constructor argument.
"""

from __future__ import annotations

import pytest
from google.adk.tools import FunctionTool
from google_adk_algenta import AlgentaToolset, ExecutionReceipt

from .helpers import bare_tool_context


class ReceiptWithShout(ExecutionReceipt):
    def shout_status(self) -> str:
        return self.execution_status.upper()


def execute_decision(decision_id: str, webhook_url: str) -> dict:
    return {"decision_id": decision_id, "webhook_url": webhook_url, "execution_status": "delivered"}


@pytest.mark.anyio
async def test_run_async_validates_against_the_custom_receipt_model() -> None:
    toolset = AlgentaToolset(
        tools=[FunctionTool(execute_decision)], profile="execute", receipt_model=ReceiptWithShout
    )
    ctx = bare_tool_context()
    tools = await toolset.get_tools(ctx)

    result = await tools[0].run_async(
        args={"decision_id": "dec-1", "webhook_url": "https://example.com/hook"},
        tool_context=ctx,
    )

    assert isinstance(result, ReceiptWithShout)
    assert result.shout_status() == "DELIVERED"


@pytest.mark.anyio
async def test_default_receipt_model_is_the_base_class() -> None:
    toolset = AlgentaToolset(tools=[FunctionTool(execute_decision)], profile="execute")
    ctx = bare_tool_context()
    tools = await toolset.get_tools(ctx)

    result = await tools[0].run_async(
        args={"decision_id": "dec-1", "webhook_url": "https://example.com/hook"},
        tool_context=ctx,
    )

    assert type(result) is ExecutionReceipt
