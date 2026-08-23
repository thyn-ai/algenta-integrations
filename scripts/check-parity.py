#!/usr/bin/env python3
"""
check-parity.py — verify every Python integration package exposes the
tool-profile contract in contracts/integration-tool-contract.json.

STATUS: stub. There is nothing to check yet — python/pydantic-ai-algenta,
python/langchain-algenta, and python/litellm-algenta are all placeholder
packages with no tool-exposing code (see each package's README.md). This
script exists now so:

  1. CI (.github/workflows/ci.yml) has a stable command to run from day one,
     rather than a new workflow step being added later alongside the first
     real integration.
  2. The shape of what D1+ needs to implement is written down here, not just
     described in prose.

TODO(D1+): once python/pydantic-ai-algenta (or langchain-algenta /
litellm-algenta) actually registers tools with its framework, this script
should:
  - import each package's tool-registration entrypoint
  - ask it, for each profile in the contract ("observe", "govern", "execute",
    "full"), which tool names it would expose
  - diff that set against contracts/integration-tool-contract.json's
    "tools"/"adds_tools" for that profile (inherited via "extends")
  - fail if there's any addition, omission, or mismatched default profile
  - fail if 'execute' is reachable without the package demonstrating (via a
    test double or an explicit runtime check) that every entry in
    "execute".requires_all_of is enforced before the call reaches the engine
  - fail if 'force' / 'override_safety' (or any equivalent) appear anywhere
    in a model-facing tool schema, for any profile

Until then, this script only validates that the contract file itself is
present and well-formed JSON, so a syntax error in the contract still fails
CI rather than being silently ignored by a script nobody wired up yet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONTRACT_PATH = Path(__file__).parent.parent / "contracts" / "integration-tool-contract.json"


def main() -> int:
    if not CONTRACT_PATH.exists():
        print(f"check-parity.py: contract file missing at {CONTRACT_PATH}", file=sys.stderr)
        return 1

    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"check-parity.py: contract file is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if "profiles" not in contract:
        print("check-parity.py: contract file has no 'profiles' key", file=sys.stderr)
        return 1

    print(
        "check-parity.py: STUB — contract file is present and well-formed "
        f"({len(contract['profiles'])} profiles declared). No Python integration "
        "package implements tool exposure yet, so there is nothing further to "
        "diff against. See this file's module docstring for what D1+ must add."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
