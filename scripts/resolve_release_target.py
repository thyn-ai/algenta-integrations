#!/usr/bin/env python3
"""Map a release tag to the package it publishes, and refuse a mismatch.

WHY THIS EXISTS. Releases are tagged ``<package-name>-v<version>``, and a tag alone is not enough
to publish from: the workflow needs the package's directory, its ecosystem (PyPI or npm), and --
critically -- confirmation that the version recorded IN the package still matches the version the
tag claims. A tag can outlive the commit that produced it (a revert, a force-push, a hand-made
tag), and publishing whatever happens to be on disk under a version number it does not carry is
how a registry ends up with a 0.1.1 that is not the 0.1.1 anyone reviewed.

Package discovery is REUSED from compute_release_bumps.discover_packages() rather than restated.
A second list would drift from the first, and the drift would show up as "the release published
nothing" long after the cause.

    python3 scripts/resolve_release_target.py --tag pydantic-ai-algenta-v0.1.1 --github-output

Exit codes: 0 resolved · 2 no package matches the tag · 3 version mismatch.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from compute_release_bumps import Package, discover_packages  # noqa: E402

# PyPI and npm are the registries the two package kinds publish to.
REGISTRY_FOR_KIND = {"python": "pypi", "node": "npm"}


@dataclass(frozen=True)
class Target:
    name: str
    version: str
    kind: str
    registry: str
    directory: str


class ResolutionError(Exception):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def resolve(tag: str, packages: list[Package] | None = None) -> Target:
    """Resolve ``<name>-v<version>`` against the discovered packages.

    Matching is by exact package NAME, not by splitting on the last ``-v``: package names contain
    hyphens (``pydantic-ai-algenta``) and nothing stops one from someday containing ``-v``, so a
    positional split is a guess where an exact comparison is available.
    """
    candidates = packages if packages is not None else discover_packages()

    # Longest name first. One package name can be a prefix of another (`srv-algenta` and
    # `srv-algenta-v2`), and in that case the shorter one also matches the longer one's tag --
    # binding to it, then rejecting on a version mismatch that is really a misidentification.
    # Longest-prefix-wins is the same rule routing tables use, for the same reason.
    for package in sorted(candidates, key=lambda p: len(p.name), reverse=True):
        prefix = f"{package.name}-v"
        if not tag.startswith(prefix):
            continue
        tagged_version = tag[len(prefix) :]
        if tagged_version != package.current_version:
            raise ResolutionError(
                f"tag {tag} claims {package.name} {tagged_version}, but the package on this "
                f"commit is at {package.current_version}. Publishing would put a version number "
                f"on artifacts that do not carry it. Re-tag from the right commit.",
                3,
            )
        kind = package.kind
        registry = REGISTRY_FOR_KIND.get(kind)
        if registry is None:
            raise ResolutionError(f"{package.name} has unknown kind {kind!r}", 2)
        return Target(
            name=package.name,
            version=tagged_version,
            kind=kind,
            registry=registry,
            directory=str(package.dir.relative_to(REPO_ROOT)),
        )

    known = ", ".join(sorted(p.name for p in candidates)) or "(none discovered)"
    raise ResolutionError(
        f"no package matches tag {tag!r}. Discovered packages: {known}. "
        "Release tags must be <package-name>-v<version>.",
        2,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="append key=value lines to $GITHUB_OUTPUT as well as printing them",
    )
    args = parser.parse_args()

    try:
        target = resolve(args.tag)
    except ResolutionError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return exc.code

    lines = [
        f"name={target.name}",
        f"version={target.version}",
        f"kind={target.kind}",
        f"registry={target.registry}",
        f"directory={target.directory}",
    ]
    for line in lines:
        print(line)

    if args.github_output:
        output = os.environ.get("GITHUB_OUTPUT")
        if not output:
            print("::error::--github-output given but GITHUB_OUTPUT is unset", file=sys.stderr)
            return 1
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
