"""`AlgentaToolset(receipt_model=...)` must actually be the model `call_tool` validates
against -- not just an accepted-but-ignored constructor argument.
"""

from __future__ import annotations

from smolagents import Tool
from smolagents_algenta import AlgentaToolset, ExecutionReceipt


class ReceiptWithShout(ExecutionReceipt):
    def shout_status(self) -> str:
        return self.execution_status.upper()


class _ExecuteDecision(Tool):
    name = "execute_decision"
    description = "Execute."
    inputs = {
        "decision_id": {"type": "string", "description": "id"},
        "webhook_url": {"type": "string", "description": "url"},
    }
    output_type = "object"
    skip_forward_signature_validation = True

    def forward(self, decision_id: str, webhook_url: str) -> dict:
        return {"decision_id": decision_id, "webhook_url": webhook_url, "execution_status": "delivered"}


def test_call_tool_validates_against_the_custom_receipt_model() -> None:
    with AlgentaToolset(
        wrapped=[_ExecuteDecision()], profile="execute", receipt_model=ReceiptWithShout
    ) as toolset:
        tool = toolset.tools[0]
        result = tool.forward(decision_id="dec-1", webhook_url="https://example.com/hook")

        assert isinstance(result, ReceiptWithShout)
        assert result.shout_status() == "DELIVERED"


def test_default_receipt_model_is_the_base_class() -> None:
    with AlgentaToolset(wrapped=[_ExecuteDecision()], profile="execute") as toolset:
        tool = toolset.tools[0]
        result = tool.forward(decision_id="dec-1", webhook_url="https://example.com/hook")

        assert type(result) is ExecutionReceipt
