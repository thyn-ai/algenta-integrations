#!/usr/bin/env python3
"""
Regression test for compute_release_bumps.py.

Builds real throwaway git repos under a temp directory (never committed, never
this repository), invokes the real script as a subprocess against real commits
and real tags, and asserts the exact behavior a live auto-release.yml run
depends on: bump-type classification (feat/fix/breaking), the bootstrap path for
a never-tagged package, convergence after tagging, per-package independence, and
that --apply actually rewrites both a Python pyproject.toml and a Node
package.json correctly.

This exists because a subtle bug here is expensive to find in production: it
either double-bumps a version, fails to bump one that should have, or -- the
real bug caught while building this -- makes the CI step that commits the bump
abort outright on the very first (all-bootstrap) run, since `git commit` exits
nonzero when there is nothing staged. See git history for why that scenario
specifically is exercised below.

Run directly:  python3 scripts/test_compute_release_bumps.py
Also wired into .github/workflows/ci.yml.

Uses only the standard library on purpose, same as test_check_no_engine_dependency.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "compute_release_bumps.py"

FAILURES: list[str] = []


def expect(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"ok:   {message}")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")


def _write_python_package(root: Path, name: str, version: str) -> Path:
    pkg_dir = root / "python" / name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n'
    )
    (pkg_dir / "README.md").write_text(f"# {name}\n")
    return pkg_dir


def _write_node_package(root: Path, name: str, version: str) -> Path:
    pkg_dir = root / "typescript" / "algenta-tools" / "packages" / name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.json").write_text(
        json.dumps({"name": name, "version": version}, indent=2) + "\n"
    )
    return pkg_dir


def _commit_all(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def run_script(root: Path, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--json", *extra_args],
        cwd=root,
        capture_output=True,
        text=True,
    )


def _bumps_by_name(result: subprocess.CompletedProcess) -> dict:
    return {b["name"]: b for b in json.loads(result.stdout)}


def test_never_tagged_package_with_no_commits_gets_no_bump() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-a", "0.0.1")
        _commit_all(root, "chore: scaffold fixture-a")

        result = run_script(root)
        expect(result.returncode == 0, f"exits 0, got {result.returncode}: {result.stderr}")
        bumps = _bumps_by_name(result)
        expect(bumps["fixture-a"]["bumped"] is False, "no bump for a scaffold-only package")


def test_bootstrap_tags_current_version_without_rewriting_it() -> None:
    """A never-tagged package WITH real feat/fix history bootstrap-releases at its
    current on-disk version, unchanged -- it does not additionally bump on top,
    since the current version may already reflect a hand-set bump made before
    this script existed (exactly what happened for real in algenta-integrations:
    D1-D3 each manually bumped 0.0.1 -> 0.1.0 when landing their implementation)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-b", "0.1.0")
        _commit_all(root, "chore: scaffold fixture-b")
        (root / "python" / "fixture-b" / "README.md").write_text("real feature\n")
        _commit_all(root, "feat(fixture-b): a real feature landed by hand before this script existed")

        result = run_script(root, "--apply")
        expect(result.returncode == 0, f"exits 0, got {result.returncode}: {result.stderr}")
        bumps = _bumps_by_name(result)
        b = bumps["fixture-b"]
        expect(b["bumped"] is True, "bootstrap counts as a release")
        expect(b["bump_kind"] == "bootstrap", f"bump_kind is bootstrap, got {b.get('bump_kind')}")
        expect(b["new_version"] == "0.1.0", f"stays at 0.1.0, got {b['new_version']}")
        on_disk = (root / "python" / "fixture-b" / "pyproject.toml").read_text()
        expect('version = "0.1.0"' in on_disk, "--apply did not rewrite the bootstrap version")


def test_feat_commit_since_tag_bumps_minor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-c", "1.2.3")
        _commit_all(root, "chore: scaffold fixture-c")
        _git(root, "tag", "fixture-c-v1.2.3")
        (root / "python" / "fixture-c" / "README.md").write_text("more\n")
        _commit_all(root, "feat(fixture-c): add a capability")

        result = run_script(root, "--apply")
        bumps = _bumps_by_name(result)
        b = bumps["fixture-c"]
        expect(b["bump_kind"] == "minor", f"feat -> minor, got {b.get('bump_kind')}")
        expect(b["new_version"] == "1.3.0", f"1.2.3 -> 1.3.0, got {b['new_version']}")
        on_disk = (root / "python" / "fixture-c" / "pyproject.toml").read_text()
        expect('version = "1.3.0"' in on_disk, "--apply rewrote the pyproject.toml version")


def test_fix_commit_since_tag_bumps_patch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-d", "1.2.3")
        _commit_all(root, "chore: scaffold fixture-d")
        _git(root, "tag", "fixture-d-v1.2.3")
        (root / "python" / "fixture-d" / "README.md").write_text("more\n")
        _commit_all(root, "fix(fixture-d): correct a bug")

        result = run_script(root, "--apply")
        bumps = _bumps_by_name(result)
        b = bumps["fixture-d"]
        expect(b["bump_kind"] == "patch", f"fix -> patch, got {b.get('bump_kind')}")
        expect(b["new_version"] == "1.2.4", f"1.2.3 -> 1.2.4, got {b['new_version']}")


def test_bang_suffix_bumps_major_even_with_a_feat_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-e", "1.2.3")
        _commit_all(root, "chore: scaffold fixture-e")
        _git(root, "tag", "fixture-e-v1.2.3")
        (root / "python" / "fixture-e" / "README.md").write_text("a\n")
        _commit_all(root, "feat(fixture-e): a normal feature")
        (root / "python" / "fixture-e" / "README.md").write_text("b\n")
        _commit_all(root, "feat(fixture-e)!: a breaking one")

        result = run_script(root)
        bumps = _bumps_by_name(result)
        b = bumps["fixture-e"]
        expect(b["bump_kind"] == "major", f"`!` -> major even alongside a feat, got {b.get('bump_kind')}")
        expect(b["new_version"] == "2.0.0", f"1.2.3 -> 2.0.0, got {b['new_version']}")


def test_breaking_change_footer_bumps_major() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-f", "1.0.0")
        _commit_all(root, "chore: scaffold fixture-f")
        _git(root, "tag", "fixture-f-v1.0.0")
        (root / "python" / "fixture-f" / "README.md").write_text("a\n")
        _git(root, "add", "-A")
        _git(
            root,
            "commit",
            "-q",
            "-m",
            "fix(fixture-f): small change\n\nBREAKING CHANGE: removed an old field",
        )

        result = run_script(root)
        bumps = _bumps_by_name(result)
        b = bumps["fixture-f"]
        expect(
            b["bump_kind"] == "major",
            f"a BREAKING CHANGE footer bumps major even on a fix: subject, got {b.get('bump_kind')}",
        )


def test_no_qualifying_commits_since_tag_means_no_bump() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-g", "1.0.0")
        _commit_all(root, "chore: scaffold fixture-g")
        _git(root, "tag", "fixture-g-v1.0.0")
        (root / "python" / "fixture-g" / "README.md").write_text("typo fix\n")
        _commit_all(root, "docs(fixture-g): fix a typo")

        result = run_script(root)
        bumps = _bumps_by_name(result)
        expect(bumps["fixture-g"]["bumped"] is False, "a docs: commit alone doesn't warrant a release")


def test_converges_to_no_bump_immediately_after_tagging() -> None:
    """The exact property auto-release.yml's convergence relies on: once a bump
    is applied and tagged, an immediate re-run against that same HEAD computes no
    further bump for that package."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-h", "0.1.0")
        _commit_all(root, "chore: scaffold fixture-h")
        (root / "python" / "fixture-h" / "README.md").write_text("feature\n")
        _commit_all(root, "feat(fixture-h): a feature")

        first = run_script(root, "--apply")
        b = _bumps_by_name(first)["fixture-h"]
        expect(b["bumped"] is True, "first run bootstraps a real release")
        _git(root, "tag", b["tag"])

        second = run_script(root)
        expect(
            _bumps_by_name(second)["fixture-h"]["bumped"] is False,
            "immediately after tagging, the same HEAD computes no further bump",
        )


def test_packages_are_independent() -> None:
    """One package's feat: commit must never bump a sibling package that has no
    commits of its own -- version-file globbing must be scoped per-package, not
    repo-wide."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-i", "1.0.0")
        _write_python_package(root, "fixture-j", "1.0.0")
        _commit_all(root, "chore: scaffold both")
        _git(root, "tag", "fixture-i-v1.0.0")
        _git(root, "tag", "fixture-j-v1.0.0")
        (root / "python" / "fixture-i" / "README.md").write_text("only i changes\n")
        _commit_all(root, "feat(fixture-i): only touches fixture-i")

        result = run_script(root)
        bumps = _bumps_by_name(result)
        expect(bumps["fixture-i"]["bumped"] is True, "fixture-i has a real feat: commit")
        expect(bumps["fixture-j"]["bumped"] is False, "fixture-j must stay untouched -- no commits of its own")


def test_node_package_json_is_rewritten_correctly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_node_package(root, "fixture-ts", "2.0.0")
        _commit_all(root, "chore: scaffold fixture-ts")
        _git(root, "tag", "fixture-ts-v2.0.0")
        pkg_json = root / "typescript" / "algenta-tools" / "packages" / "fixture-ts" / "package.json"
        pkg_json.write_text(json.dumps({"name": "fixture-ts", "version": "2.0.0", "extra": "field"}, indent=2))
        _commit_all(root, "fix(fixture-ts): a bugfix")

        result = run_script(root, "--apply")
        bumps = _bumps_by_name(result)
        b = bumps["fixture-ts"]
        expect(b["new_version"] == "2.0.1", f"2.0.0 -> 2.0.1, got {b['new_version']}")
        data = json.loads(pkg_json.read_text())
        expect(data["version"] == "2.0.1", "package.json's version field was rewritten")
        expect(data["extra"] == "field", "unrelated package.json fields are preserved (no full rewrite)")


def test_dry_run_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_repo(root)
        _write_python_package(root, "fixture-k", "1.0.0")
        _commit_all(root, "chore: scaffold fixture-k")
        _git(root, "tag", "fixture-k-v1.0.0")
        (root / "python" / "fixture-k" / "README.md").write_text("x\n")
        _commit_all(root, "feat(fixture-k): a feature")

        before = (root / "python" / "fixture-k" / "pyproject.toml").read_text()
        result = run_script(root)  # no --apply
        expect(result.returncode == 0, "dry run still exits 0")
        after = (root / "python" / "fixture-k" / "pyproject.toml").read_text()
        expect(before == after, "without --apply, no file is written")


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
