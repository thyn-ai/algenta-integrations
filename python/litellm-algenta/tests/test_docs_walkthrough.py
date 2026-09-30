"""Docs-tested harness for `docs/gateway-profile-enforcement.md`.

Every ``bash`` fenced code block in the walkthrough is concatenated and executed
in a single shell session against the workspace interpreter, so the page cannot
silently rot. The live sections use this package's deterministic stub server and
proxy fixtures; no real Algenta engine, no LLM provider key, and no external
network access are required.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_DOCS_PATH = Path(__file__).resolve().parent.parent / "docs" / "gateway-profile-enforcement.md"
_PYTHON_DIR = Path(__file__).resolve().parents[2]


def _extract_bash_blocks(markdown: str) -> list[str]:
    """Return the content of every `` ```bash `` fenced block in *markdown*."""
    pattern = re.compile(r"^```bash\s*\n(.*?)```", re.MULTILINE | re.DOTALL)
    return pattern.findall(markdown)


def test_docs_walkthrough_executes() -> None:
    """The walkthrough docs page's bash blocks must run end-to-end without error."""
    assert _DOCS_PATH.exists(), f"walkthrough docs page missing: {_DOCS_PATH}"
    markdown = _DOCS_PATH.read_text(encoding="utf-8")
    blocks = _extract_bash_blocks(markdown)
    assert blocks, "no bash code blocks found in walkthrough docs"

    script = "\n\n".join(blocks)

    env = os.environ.copy()
    # Ensure `python` in the extracted blocks resolves to the same interpreter
    # that is running this test (the workspace venv). This lets the docs use the
    # plain `python ...` spelling instead of the slower `uv run python ...`.
    python_dir = str(Path(sys.executable).parent)
    env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        ["bash", "-e", "-u", "-o", "pipefail", "-c", script],
        cwd=_PYTHON_DIR,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"docs walkthrough shell script failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    stdout = result.stdout
    assert "Read-only situational awareness" in stdout
    assert "allowed_tools: ['get_contract', 'query_data', 'recommend', 'simulate']" in stdout
    assert (
        "HTTP 403 -- refused by the gateway itself (execute_decision is not in allowed_tools)"
    ) in stdout
    assert (
        "HTTP 403 -- refused by the gateway itself "
        "('force' is not in allowed_params.execute_decision)"
    ) in stdout
    assert "ExecutionReceipt: decision_id=walkthrough-delivered" in stdout
    assert "lint passed" in stdout
