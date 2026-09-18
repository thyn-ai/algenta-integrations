#!/usr/bin/env python3
"""Compute per-package semantic-version bumps from Conventional Commits since each
package's last release tag, and (with --apply) write the new version into each
package's own version file.

Replaces release-please's PR-based release flow. release-please's whole job is to
open a pull request, and this org's ENTERPRISE policy disables Actions creating or
approving pull requests -- see .github/workflows/release-please.yml's own header
(kept for context, no longer wired into ci) for the full story. Rather than fight
that policy with an App-token workaround for every merge, this script gives the
same "automated, never hand-run" bump [feedback_no_manual_release_pushes] without
ever needing a pull request at all: a workflow runs this with --apply directly
after a merge to main and pushes the resulting commit + tags straight to main,
mirroring the proven `thyn-ai/codna` auto-release-cli.yml pattern (zero PR, direct
push, convergent).

Convergence, simpler than codna's: codna reconciles against PyPI (an external,
laggy, cache-prone truth source), which needed an explicit "is HEAD my own bump
commit" guard to terminate. This repo doesn't publish anywhere yet (see
release-please.yml's original header -- an explicit, separate, owner-gated
decision for later), so the only state being reconciled is "do this package's git
tags already account for every commit that touches it". That is self-consistent:
the moment a bump commit is tagged, the tag itself becomes the new boundary, so the
immediate re-trigger from that push sees zero commits since the (now up to date)
tag and computes no further bump. No self-commit detection needed.

Packages are DISCOVERED (python/*/pyproject.toml, typescript/*/packages/*/package.json),
not hardcoded, matching this repo's ci.yml convention of glob-discovering packages so a
newly scaffolded package needs no edit here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def _find_repo_root() -> Path:
    """The git repository root of the CURRENT WORKING DIRECTORY, not wherever this
    script file happens to live. Deliberately NOT `Path(__file__).resolve().parent.parent`
    -- that resolves to this script's own location, which happens to be correct in
    production (invoked as `python3 scripts/compute_release_bumps.py` from within
    the repo it operates on) but silently ignores the intended target repo for any
    other invocation, including every fixture-based test in
    test_compute_release_bumps.py, which run this script against throwaway temp
    repos via `cwd=root` -- a real bug caught exactly that way while building this.
    """
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


REPO_ROOT = _find_repo_root()

# Directories that are examples/scaffolds, never released.
EXCLUDED_PYTHON_DIRS = {"examples"}


@dataclass
class Package:
    name: str
    dir: Path
    version_file: Path
    kind: str  # "python" | "node"
    current_version: str


def _run(*args: str, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def discover_packages() -> list[Package]:
    packages: list[Package] = []

    for pyproject in sorted((REPO_ROOT / "python").glob("*/pyproject.toml")):
        pkg_dir = pyproject.parent
        if pkg_dir.name in EXCLUDED_PYTHON_DIRS:
            continue
        text = pyproject.read_text()
        name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
        version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        if not name_match or not version_match:
            continue
        packages.append(
            Package(
                name=name_match.group(1),
                dir=pkg_dir,
                version_file=pyproject,
                kind="python",
                current_version=version_match.group(1),
            )
        )

    for package_json in sorted((REPO_ROOT / "typescript").glob("*/packages/*/package.json")):
        pkg_dir = package_json.parent
        data = json.loads(package_json.read_text())
        name = data.get("name")
        version = data.get("version")
        if not name or not version:
            continue
        packages.append(
            Package(
                name=name,
                dir=pkg_dir,
                version_file=package_json,
                kind="node",
                current_version=version,
            )
        )

    return packages


def latest_tag_for(package_name: str) -> str | None:
    try:
        out = _run(
            "git",
            "tag",
            "-l",
            f"{package_name}-v*",
            "--sort=-v:refname",
        )
    except subprocess.CalledProcessError:
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    return lines[0] if lines else None


_BREAKING_RE = re.compile(r"BREAKING[ -]CHANGE", re.IGNORECASE)
_CONVENTIONAL_TYPE_RE = re.compile(r"^(\w+)(\([^)]*\))?(!)?:\s", re.MULTILINE)


def bump_kind_for(package: Package, since_tag: str | None) -> str | None:
    """Return "major" | "minor" | "patch" | None (no bump warranted)."""
    rel_dir = package.dir.relative_to(REPO_ROOT).as_posix()
    commit_range = f"{since_tag}..HEAD" if since_tag else "HEAD"
    # %B = full raw commit message (subject + body), separated by a NUL so a
    # multi-line body can't be mistaken for a second commit.
    out = _run(
        "git", "log", commit_range, "--format=%B%x00", "--", rel_dir
    )
    messages = [m for m in out.split("\x00") if m.strip()]
    if not messages:
        return None

    highest: str | None = None
    for message in messages:
        first_line = message.splitlines()[0] if message.splitlines() else ""
        type_match = _CONVENTIONAL_TYPE_RE.match(first_line + " ")
        is_breaking = bool(_BREAKING_RE.search(message)) or bool(
            type_match and type_match.group(3) == "!"
        )
        if is_breaking:
            return "major"  # can't get higher than major, short-circuit
        if type_match and type_match.group(1) == "feat":
            highest = "minor" if highest != "major" else highest
        elif type_match and type_match.group(1) == "fix":
            if highest is None:
                highest = "patch"
    return highest


def apply_bump(version: str, kind: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump kind {kind!r}")


def write_new_version(package: Package, new_version: str) -> None:
    text = package.version_file.read_text()
    if package.kind == "python":
        updated, count = re.subn(
            r'(?m)^version\s*=\s*"' + re.escape(package.current_version) + r'"',
            f'version = "{new_version}"',
            text,
            count=1,
        )
        if count != 1:
            raise RuntimeError(f"could not find version line to replace in {package.version_file}")
        package.version_file.write_text(updated)
    else:
        data = json.loads(text)
        data["version"] = new_version
        package.version_file.write_text(json.dumps(data, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write version files (default: dry run).")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON (for the workflow step)."
    )
    args = parser.parse_args()

    packages = discover_packages()
    if not packages:
        print("No packages discovered -- discovery globs are wrong.", file=sys.stderr)
        return 1

    results = []
    for package in packages:
        since_tag = latest_tag_for(package.name)
        kind = bump_kind_for(package, since_tag)

        if since_tag is None:
            # Bootstrap: no release for this package exists yet. The on-disk version may
            # already reflect real work an implementer set by hand before this script
            # existed (e.g. D1-D3 each hand-bumped 0.0.1 -> 0.1.0 when landing their real
            # implementation) -- bumping again on top of that would double-count commits
            # already accounted for. So: if there's release-worthy history at all, the
            # bootstrap release is simply "tag the current on-disk version as the first
            # release" (no file write); a package with no qualifying commits yet (still a
            # bare scaffold, e.g. litellm-algenta pre-D4) gets no tag and no release at all
            # -- there is nothing to release.
            if kind is None:
                results.append(
                    {
                        "name": package.name,
                        "bumped": False,
                        "current_version": package.current_version,
                        "since_tag": None,
                    }
                )
                continue
            results.append(
                {
                    "name": package.name,
                    "bumped": True,
                    "bump_kind": "bootstrap",
                    "current_version": package.current_version,
                    "new_version": package.current_version,
                    "since_tag": None,
                    "tag": f"{package.name}-v{package.current_version}",
                }
            )
            continue

        if kind is None:
            results.append(
                {
                    "name": package.name,
                    "bumped": False,
                    "current_version": package.current_version,
                    "since_tag": since_tag,
                }
            )
            continue
        new_version = apply_bump(package.current_version, kind)
        if args.apply:
            write_new_version(package, new_version)
        results.append(
            {
                "name": package.name,
                "bumped": True,
                "bump_kind": kind,
                "current_version": package.current_version,
                "new_version": new_version,
                "since_tag": since_tag,
                "tag": f"{package.name}-v{new_version}",
            }
        )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            if result["bumped"]:
                since = result["since_tag"] or "the beginning"
                print(
                    f"{result['name']}: {result['current_version']} -> {result['new_version']}"
                    f" ({result['bump_kind']}, commits since {since})"
                )
            else:
                print(f"{result['name']}: no release-worthy commits since {result['since_tag'] or 'the beginning'} -- no bump")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
