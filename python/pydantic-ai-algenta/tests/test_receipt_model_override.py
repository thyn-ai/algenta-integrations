"""`AlgentaToolset(receipt_model=...)` must actually be the model `call_tool` validates
against -- not just an accepted-but-ignored constructor argument.
"""

from __future__ import annotations

import pytest
from pydantic_ai.toolsets.function import FunctionToolset

from pydantic_ai_algenta import AlgentaToolset, GovernedExecutionReceipt

from .helpers import bare_run_context


class ReceiptWithShout(GovernedExecutionReceipt):
    def shout_code(self) -> str:
        return self.code.upper()


def recommend(scenario: str) -> dict:
    return {"status": "ok", "code": "ok", "approval_state": "none", "result": {"scenario": scenario}}


@pytest.mark.anyio
async def test_call_tool_validates_against_the_custom_receipt_model() -> None:
    toolset = AlgentaToolset(
        wrapped=FunctionToolset([recommend]), profile="observe", receipt_model=ReceiptWithShout
    )
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)

    result = await toolset.call_tool("recommend", {"scenario": "x"}, ctx, tools["recommend"])

    assert isinstance(result, ReceiptWithShout)
    assert result.shout_code() == "OK"


@pytest.mark.anyio
async def test_default_receipt_model_is_the_base_class() -> None:
    toolset = AlgentaToolset(wrapped=FunctionToolset([recommend]), profile="observe")
    ctx = bare_run_context()
    tools = await toolset.get_tools(ctx)

    result = await toolset.call_tool("recommend", {"scenario": "x"}, ctx, tools["recommend"])

    assert type(result) is GovernedExecutionReceipt
