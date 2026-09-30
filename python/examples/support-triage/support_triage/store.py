"""Local receipt persistence for the governed support-triage example."""

from __future__ import annotations

from pathlib import Path

from langchain_algenta import ExecutionReceipt


class ReceiptStore:
    """Write execution receipts to a local directory as JSON files.

    Each receipt is stored at ``<directory>/<decision_id>.json``. The store is
    intentionally simple: this example demonstrates the persist step of the arc
    without depending on any proprietary or hosted storage service.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def persist(self, receipt: ExecutionReceipt) -> Path:
        """Persist ``receipt`` to disk and return the path written."""
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{receipt.decision_id}.json"
        path.write_text(receipt.model_dump_json(indent=2), encoding="utf-8")
        return path
