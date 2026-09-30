"""Command-line entry point for the governed support-triage example.

Run with no setup against the local stub server (zero external services):

    cd algenta-integrations/python
    uv sync --all-packages --all-extras
    uv run --package support-triage python -m support_triage

To point at your own self-hosted Algenta engine instead, set ``ALGENTA_BASE_URL``
before running; the stub server is skipped when the variable is present.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from support_triage._stub import StubServerFixture
from support_triage.agent import run_triage


async def _interactive_approval(decision_record: dict[str, Any]) -> bool:
    prompt = f"Approve execution of {decision_record['decision_id']!r}? [y/N] "
    answer = input(prompt)
    return answer.strip().lower() == "y"


async def _auto_approve(_decision_record: dict[str, Any]) -> bool:
    """Auto-approve for the zero-setup demo; the test suite injects its own gate."""
    return True


def _demo_tickets() -> list[dict[str, Any]]:
    """A small deterministic queue for the local demo."""
    return [
        {
            "id": "T-1",
            "scenario": "customer received a damaged widget and wants a refund",
            "action": "refund",
        },
        {
            "id": "T-2",
            "scenario": "enterprise account requests expedited onboarding",
            "action": "escalate",
        },
    ]


async def main() -> None:
    base_url = os.environ.get("ALGENTA_BASE_URL")
    receipts_dir = Path.cwd() / "receipts"
    if base_url:
        print(f"Using self-hosted engine at {base_url}")
        await run_triage(
            base_url,
            _demo_tickets(),
            approval_gate=_interactive_approval,
            receipts_dir=receipts_dir,
        )
        return

    async with StubServerFixture() as stub:
        print(f"Stub Algenta MCP server listening at {stub.base_url}")
        print("Processing demo tickets (all approvals granted automatically)...")
        final_state = await run_triage(
            stub.base_url,
            _demo_tickets(),
            approval_gate=_auto_approve,
            receipts_dir=receipts_dir,
        )
        print(f"Persisted {len(final_state.get('persisted_paths', []))} receipt(s) to ./receipts/")


if __name__ == "__main__":
    asyncio.run(main())
