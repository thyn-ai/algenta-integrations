#!/usr/bin/env python3
"""
Regression test for check-no-engine-dependency.py.

This is the permanent proof that the check actually catches a violation,
not just a script that was eyeballed once and trusted. It builds a handful
of throwaway fixture repos under a temp directory (never committed, never a
real dependency of this repository), runs the real checker against each as
a subprocess, and asserts the exit code / findings are what they should be.

Run directly:  python scripts/test_check_no_engine_dependency.py
Also wired into .github/workflows/ci.yml on every PR.

Uses only the standard library on purpose — this is a CI gate for a
dependency-hygiene script, so it must not itself acquire new dependencies.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

CHECKER = Path(__file__).parent / "check-no-engine-dependency.py"
REPO_ROOT = Path(__file__).parent.parent

FAILURES: list[str] = []


def run_checker(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok:   {message}")


def test_catches_file_path_engine_dependency() -> None:
    """The canonical violation this gate exists to catch: a package.json that
    depends on the closed engine's repository via a local file: path."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "package.json").write_text(
            """
            {
              "name": "fixture-violation",
              "version": "0.0.0",
              "dependencies": {
                "algenta-sdk": "^1.0.11",
                "decision-engine": "file:../../decision-engine"
              }
            }
            """
        )
        result = run_checker(root)
        expect(result.returncode == 1, "deliberately-violating package.json is REJECTED (exit code 1)")
        expect("decision-engine" in result.stdout, "violation output names the offending dependency")
        expect("file:../../decision-engine" in result.stdout or "banned target" in result.stdout,
               "violation output explains why (local path / banned target)")


def test_catches_mojo_python_import() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "bad.py").write_text("import mojo.engine.kernels as kernels\n")
        result = run_checker(root)
        expect(result.returncode == 1, "a Python file importing mojo.* is REJECTED")


def test_catches_apps_api_server_import() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "bad.py").write_text("from apps.api_server.config import Settings\n")
        result = run_checker(root)
        expect(result.returncode == 1, "a Python file importing apps.api_server is REJECTED")


def test_catches_relative_path_escaping_repo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pkg").mkdir()
        (root / "pkg" / "bad.ts").write_text('import { engine } from "../../../../decision-engine/mojo/engine";\n')
        result = run_checker(root)
        expect(result.returncode == 1, "a TS import path that escapes the repo (and names the engine) is REJECTED")


def test_allows_legitimate_sdk_python_import() -> None:
    """The published algenta-sdk's own import namespace is `decision_engine`
    (underscore) -- this must NOT be flagged, or the check would reject the
    one thing this whole repo is supposed to depend on."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "good.py").write_text("from decision_engine import AlgentaClient\n")
        result = run_checker(root)
        expect(result.returncode == 0, "legitimate `from decision_engine import AlgentaClient` (the published SDK) is ALLOWED")


def test_allows_published_sdk_dependency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(
            """
            [project]
            name = "fixture-clean"
            version = "0.0.0"
            dependencies = ["algenta-sdk>=1.0.11", "pydantic>=2.7.0"]
            """
        )
        (root / "package.json").write_text(
            """
            {
              "name": "fixture-clean",
              "version": "0.0.0",
              "dependencies": { "algenta-sdk": "^1.0.11" }
            }
            """
        )
        result = run_checker(root)
        expect(result.returncode == 0, "a clean manifest depending only on algenta-sdk (+ ordinary deps) PASSES")


def test_real_repo_is_currently_clean() -> None:
    """The actual repository this test lives in must pass its own gate."""
    result = run_checker(REPO_ROOT)
    expect(result.returncode == 0, f"the real repository at {REPO_ROOT} passes the check")
    if result.returncode != 0:
        print(result.stdout)


def main() -> int:
    test_catches_file_path_engine_dependency()
    test_catches_mojo_python_import()
    test_catches_apps_api_server_import()
    test_catches_relative_path_escaping_repo()
    test_allows_legitimate_sdk_python_import()
    test_allows_published_sdk_dependency()
    test_real_repo_is_currently_clean()

    if FAILURES:
        print(f"\n{len(FAILURES)} test(s) failed.")
        return 1
    print("\nAll check-no-engine-dependency regression tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
