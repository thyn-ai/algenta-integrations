#!/usr/bin/env python3
"""Regression tests for sync-on-sdk-release's registry-version handling."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/sync-on-sdk-release.yml"
STEP_NAME = "Determine target algenta-sdk version"
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")


def workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def step_block(name: str) -> str:
    marker = f"      - name: {name}\n"
    check(marker in workflow_text(), f"missing workflow step: {name}")
    if marker not in workflow_text():
        return ""
    remainder = workflow_text().split(marker, 1)[1]
    return remainder.split("\n      - name:", 1)[0]


def version_script() -> str:
    block = step_block(STEP_NAME)
    marker = "        run: |\n"
    check(marker in block, f"{STEP_NAME}: missing run block")
    if marker not in block:
        return ""
    lines = block.split(marker, 1)[1].splitlines()
    script_lines: list[str] = []
    for line in lines:
        if not line.strip():
            script_lines.append("")
            continue
        check(line.startswith("          "), f"unexpected indentation: {line!r}")
        script_lines.append(line[10:] if line.startswith("          ") else line)
    return "\n".join(script_lines) + "\n"


def run_version_step(
    registry_status: int,
    registry_version: str = "",
    dispatch_version: str = "",
    dispatch_tag: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        binary_directory = root / "bin"
        binary_directory.mkdir()
        python_stub = binary_directory / "python"
        python_stub.write_text(
            "#!/bin/sh\n"
            "if [ -n \"${REGISTRY_VERSION:-}\" ]; then\n"
            "  printf '%s\\n' \"$REGISTRY_VERSION\"\n"
            "fi\n"
            "exit \"${REGISTRY_STATUS:-0}\"\n",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)
        output_path = root / "github-output"
        output_path.touch()
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{binary_directory}:{environment['PATH']}",
                "GITHUB_OUTPUT": str(output_path),
                "REGISTRY_STATUS": str(registry_status),
                "REGISTRY_VERSION": registry_version,
                "DISPATCH_VERSION": dispatch_version,
                "DISPATCH_TAG": dispatch_tag,
            }
        )
        result = subprocess.run(
            ["bash", "-c", version_script()],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        output_lines = output_path.read_text(encoding="utf-8").splitlines()
        outputs = dict(line.split("=", 1) for line in output_lines)
        return result, outputs


def test_registry_mismatch_skips_without_a_target() -> None:
    result, outputs = run_version_step(registry_status=2)
    check(result.returncode == 0, f"mismatch exited {result.returncode}: {result.stderr}")
    check(outputs == {"available": "false"}, f"unexpected mismatch outputs: {outputs}")
    check("::notice::" in result.stdout, "mismatch did not emit a workflow notice")


def test_registry_failure_remains_fatal() -> None:
    result, _ = run_version_step(registry_status=1)
    check(result.returncode == 1, f"registry failure exited {result.returncode}")


def test_matching_registries_produce_a_target() -> None:
    result, outputs = run_version_step(registry_status=0, registry_version="1.2.3")
    check(result.returncode == 0, f"matching registries exited {result.returncode}")
    check(outputs.get("available") == "true", f"unexpected outputs: {outputs}")
    check(outputs.get("version") == "1.2.3", f"unexpected version: {outputs}")
    check(outputs.get("source") == "scheduled check of PyPI + npm", f"unexpected source: {outputs}")


def test_dispatch_bypasses_registry_lookup() -> None:
    result, outputs = run_version_step(
        registry_status=99,
        dispatch_version="2.3.4",
        dispatch_tag="sdk-v2.3.4",
    )
    check(result.returncode == 0, f"dispatch exited {result.returncode}")
    check(outputs.get("available") == "true", f"unexpected outputs: {outputs}")
    check(outputs.get("version") == "2.3.4", f"unexpected version: {outputs}")
    check(outputs.get("source") == "repository_dispatch (sdk-v2.3.4)", f"unexpected source: {outputs}")


def test_registry_backed_steps_require_an_available_target() -> None:
    for name in ("Bump every package's algenta-sdk pin", "Check whether anything actually changed"):
        block = step_block(name)
        check(
            "if: steps.version.outputs.available == 'true'" in block,
            f"{name}: not gated on an available target",
        )


def main() -> int:
    tests = [
        obj
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    for test in tests:
        print(f"--- {test.__name__} ---")
        test()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
