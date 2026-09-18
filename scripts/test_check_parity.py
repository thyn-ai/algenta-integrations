#!/usr/bin/env python3
"""
Regression tests for check-parity.py.

This is the permanent proof that the parity gate actually catches drift, not
just a script that was eyeballed once and trusted. Each test builds a
throwaway fixture repository under a temp directory (a contract file plus one
or two embedded contract modules, in the same declarative shape as the real
packages), runs the real checker against it as a subprocess, and asserts the
exit code and the wording of the finding. One test runs the checker against
this repository itself and requires a clean result.

Run directly:   python scripts/test_check_parity.py
Run via pytest: uv run pytest scripts/test_check_parity.py   (from python/)

Uses only the standard library on purpose — this is a CI gate for a
contract-drift script, so it must not itself acquire new dependencies. Tests
are plain `assert`-based functions so failures are real failures under both
runners (pytest collects each `test_*` function; `main()` runs them all and
returns a process exit code).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

CHECKER = Path(__file__).parent / "check-parity.py"
REPO_ROOT = Path(__file__).parent.parent
REAL_CONTRACT_PATH = REPO_ROOT / "contracts" / "integration-tool-contract.json"

ALL_TOOLS = (
    "get_contract",
    "query_data",
    "simulate",
    "recommend",
    "plan_decision",
    "log_decision",
    "execute_decision",
)


# ── fixture builders ─────────────────────────────────────────────────────────


def real_contract() -> dict:
    return json.loads(REAL_CONTRACT_PATH.read_text(encoding="utf-8"))


def write_contract(root: Path, contract: dict) -> None:
    contracts_dir = root / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (contracts_dir / "integration-tool-contract.json").write_text(
        json.dumps(contract, indent=2), encoding="utf-8"
    )


def _const_name(tool: str) -> str:
    return tool.upper()


def python_contract_module(
    *,
    observe: tuple[str, ...] = ("get_contract", "query_data", "simulate", "recommend"),
    govern_adds: tuple[str, ...] = ("plan_decision", "log_decision"),
    execute_adds: tuple[str, ...] = ("execute_decision",),
    profile_keys: tuple[str, ...] = ("observe", "govern", "execute", "full"),
    full_expression: str = "FULL_PROFILE_SENTINEL",
    default_profile: str = "observe",
    never_fields: tuple[str, ...] = ("force", "override_safety"),
) -> str:
    """Render an embedded contract module in the exact declarative shape of the real packages."""
    tools = sorted(set(ALL_TOOLS) | set(observe) | set(govern_adds) | set(execute_adds))
    tool_consts = "\n".join(f'{_const_name(tool)}: Final = "{tool}"' for tool in tools)
    literal = ", ".join(f'"{key}"' for key in profile_keys)
    observe_names = ", ".join(_const_name(tool) for tool in observe)
    govern_names = ", ".join(_const_name(tool) for tool in govern_adds)
    execute_names = ", ".join(_const_name(tool) for tool in execute_adds)
    profile_sets = ("_OBSERVE_TOOLS", "_GOVERN_TOOLS", "_EXECUTE_TOOLS", full_expression)
    profiles_body = "\n".join(
        f'    "{key}": {profile_sets[index]},' for index, key in enumerate(profile_keys)
    )
    never = ", ".join(f'"{field}"' for field in never_fields)
    return f'''"""Fixture embedded-contract module, in the real packages' declarative shape."""

from __future__ import annotations

from typing import Final, Literal

ToolProfile = Literal[{literal}]

DEFAULT_PROFILE: Final[ToolProfile] = "{default_profile}"

{tool_consts}

_OBSERVE_TOOLS: Final[frozenset[str]] = frozenset({{{observe_names}}})
_GOVERN_TOOLS: Final[frozenset[str]] = _OBSERVE_TOOLS | {{{govern_names}}}
_EXECUTE_TOOLS: Final[frozenset[str]] = _GOVERN_TOOLS | {{{execute_names}}}

FULL_PROFILE_SENTINEL: Final = "*"

TOOL_PROFILES: Final[dict[ToolProfile, frozenset[str] | Literal["*"]]] = {{
{profiles_body}
}}

NEVER_MODEL_FACING_FIELDS: Final[frozenset[str]] = frozenset({{{never}}})
'''


def typescript_contract_module(
    *,
    observe: tuple[str, ...] = ("get_contract", "query_data", "simulate", "recommend"),
    govern_adds: tuple[str, ...] = ("plan_decision", "log_decision"),
    execute_adds: tuple[str, ...] = ("execute_decision",),
    default_profile: str = "observe",
    never_fields: tuple[str, ...] = ("force", "override_safety"),
) -> str:
    """Render an embedded contract module in the exact declarative shape of the real TS packages."""
    tools = sorted(set(ALL_TOOLS) | set(observe) | set(govern_adds) | set(execute_adds))
    tool_consts = "\n".join(f"export const {_const_name(tool)} = '{tool}';" for tool in tools)
    observe_names = ", ".join(_const_name(tool) for tool in observe)
    govern_names = ", ".join(_const_name(tool) for tool in govern_adds)
    execute_names = ", ".join(_const_name(tool) for tool in execute_adds)
    never = ", ".join(f"'{field}'" for field in never_fields)
    return f"""/**
 * Fixture embedded-contract module, in the real TS packages' declarative shape.
 */

export type ToolProfile = 'observe' | 'govern' | 'execute' | 'full';

export const DEFAULT_PROFILE: ToolProfile = '{default_profile}';

{tool_consts}

const OBSERVE_TOOLS: ReadonlySet<string> = new Set([{observe_names}]);
const GOVERN_TOOLS: ReadonlySet<string> = new Set([...OBSERVE_TOOLS, {govern_names}]);
const EXECUTE_TOOLS: ReadonlySet<string> = new Set([...GOVERN_TOOLS, {execute_names}]);

export const FULL_PROFILE_SENTINEL = '*' as const;

export const TOOL_PROFILES: Readonly<Record<ToolProfile, ReadonlySet<string> | typeof FULL_PROFILE_SENTINEL>> = {{
  observe: OBSERVE_TOOLS,
  govern: GOVERN_TOOLS,
  execute: EXECUTE_TOOLS,
  full: FULL_PROFILE_SENTINEL,
}};

export const NEVER_MODEL_FACING_FIELDS: ReadonlySet<string> = new Set([{never}]);
"""


def write_python_package(
    root: Path, package_dir: str, import_name: str, module_source: str
) -> None:
    package_path = root / "python" / package_dir
    (package_path / import_name).mkdir(parents=True, exist_ok=True)
    (package_path / "pyproject.toml").write_text(
        f'[project]\nname = "{package_dir}"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (package_path / import_name / "contract.py").write_text(module_source, encoding="utf-8")


def write_typescript_package(root: Path, workspace: str, module_source: str) -> None:
    workspace_path = root / "typescript" / workspace
    package_src = workspace_path / "packages" / workspace / "src"
    package_src.mkdir(parents=True, exist_ok=True)
    (workspace_path / "package.json").write_text(
        f'{{"name": "{workspace}", "private": true}}\n', encoding="utf-8"
    )
    (package_src / "contract.ts").write_text(module_source, encoding="utf-8")


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        capture_output=True,
        text=True,
    )


# ── the tests ────────────────────────────────────────────────────────────────


def test_real_repo_passes() -> None:
    """The actual repository this test lives in must pass its own gate."""
    result = run_checker(REPO_ROOT)
    assert result.returncode == 0, (
        f"real repo must be clean, got exit {result.returncode}:\n{result.stdout}"
    )
    assert "8 embedded contract module(s) verified" in result.stdout


def test_clean_fixture_tree_passes() -> None:
    """A minimal tree with one conforming Python and one conforming TS package is clean."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(root, "fixture-py", "fixture_py", python_contract_module())
        write_typescript_package(root, "fixture-ts", typescript_contract_module())
        result = run_checker(root)
        assert result.returncode == 0, f"clean fixture must pass:\n{result.stdout}"
        assert "ok   python/fixture-py/fixture_py/contract.py" in result.stdout
        assert "ok   typescript/fixture-ts/packages/fixture-ts/src/contract.ts" in result.stdout


def test_bad_profile_name_fails() -> None:
    """A package inventing its own profile boundary is rejected, naming the profile."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root,
            "fixture-py",
            "fixture_py",
            python_contract_module(profile_keys=("observer", "govern", "execute", "full")),
        )
        result = run_checker(root)
        assert result.returncode == 1, f"bad profile name must fail with exit 1:\n{result.stdout}"
        assert "'observer'" in result.stdout
        assert "does not define" in result.stdout


def test_missing_required_tool_fails() -> None:
    """A profile omitting a contract-required tool schema is rejected, naming the tool."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root,
            "fixture-py",
            "fixture_py",
            python_contract_module(observe=("get_contract", "query_data", "recommend")),
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"a missing tool schema must fail with exit 1:\n{result.stdout}"
        )
        assert "omits" in result.stdout
        assert "'simulate'" in result.stdout


def test_invented_model_facing_tool_fails() -> None:
    """A package handing the model a tool the contract does not sanction is rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root,
            "fixture-py",
            "fixture_py",
            python_contract_module(
                observe=("get_contract", "query_data", "simulate", "recommend", "delete_everything")
            ),
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"an invented model-facing tool must fail with exit 1:\n{result.stdout}"
        )
        assert "does not assign" in result.stdout
        assert "'delete_everything'" in result.stdout


def test_wrong_default_profile_fails() -> None:
    """The default profile is one shared decision; a per-package override is rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root, "fixture-py", "fixture_py", python_contract_module(default_profile="govern")
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"a wrong default profile must fail with exit 1:\n{result.stdout}"
        )
        assert "DEFAULT_PROFILE is 'govern'" in result.stdout
        assert "'observe'" in result.stdout


def test_full_profile_must_be_sentinel_fails() -> None:
    """Enumerating 'full' as a fixed set contradicts the contract's '*' sentinel."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root,
            "fixture-py",
            "fixture_py",
            python_contract_module(full_expression="_OBSERVE_TOOLS"),
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"a non-sentinel 'full' must fail with exit 1:\n{result.stdout}"
        )
        assert "TOOL_PROFILES['full'] must be the '*' sentinel" in result.stdout


def test_never_model_facing_drift_fails() -> None:
    """Dropping a break-glass field from the strip-list would leak it model-facing; rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root,
            "fixture-py",
            "fixture_py",
            python_contract_module(never_fields=("override_safety",)),
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"never-model-facing drift must fail with exit 1:\n{result.stdout}"
        )
        assert "NEVER_MODEL_FACING_FIELDS" in result.stdout
        assert "'force'" in result.stdout


def test_typescript_violation_fails() -> None:
    """The TypeScript embedded modules are really parsed, not skipped: a TS violation fails."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_typescript_package(
            root,
            "fixture-ts",
            typescript_contract_module(
                observe=("get_contract", "query_data", "simulate", "recommend", "mine_data")
            ),
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"a TS-side violation must fail with exit 1:\n{result.stdout}"
        )
        assert "typescript/fixture-ts/packages/fixture-ts/src/contract.ts" in result.stdout
        assert "'mine_data'" in result.stdout


def test_contract_self_inconsistency_fails() -> None:
    """A contract whose profile lists name a tool absent from mcp_tool_reference is rejected."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        contract = real_contract()
        contract["profiles"]["observe"]["tools"] = [
            *contract["profiles"]["observe"]["tools"],
            "mystery_tool",
        ]
        write_contract(root, contract)
        write_python_package(root, "fixture-py", "fixture_py", python_contract_module())
        result = run_checker(root)
        assert result.returncode == 1, (
            f"a self-inconsistent contract must fail with exit 1:\n{result.stdout}"
        )
        assert "mcp_tool_reference" in result.stdout
        assert "'mystery_tool'" in result.stdout


def test_parse_failure_fails_closed() -> None:
    """A computed (non-declarative) embedded contract module is a violation, not a silent pass."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_contract(root, real_contract())
        write_python_package(
            root,
            "fixture-py",
            "fixture_py",
            "def build_profiles():\n    return {}\n\nTOOL_PROFILES = build_profiles()\n",
        )
        result = run_checker(root)
        assert result.returncode == 1, (
            f"a non-declarative module must fail closed with exit 1:\n{result.stdout}"
        )
        assert "fails closed" in result.stdout
        assert "TOOL_PROFILES" in result.stdout


def test_missing_contract_file_is_fatal() -> None:
    """Without the source of truth there is nothing to check against: exit 2, not 0 or 1."""
    with tempfile.TemporaryDirectory() as tmp:
        result = run_checker(Path(tmp))
        assert result.returncode == 2, (
            f"missing contract file must be fatal with exit 2, got {result.returncode}"
        )
        assert "contract file missing" in result.stderr


# ── direct-runner (pytest collects the functions above on its own) ───────────


def main() -> int:
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    failures = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failures += 1
            print(f"FAIL: {name}")
            traceback.print_exc()
        else:
            print(f"ok:   {name}")
    if failures:
        print(f"\n{failures} of {len(tests)} test(s) failed.")
        return 1
    print(f"\nAll {len(tests)} check-parity regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
