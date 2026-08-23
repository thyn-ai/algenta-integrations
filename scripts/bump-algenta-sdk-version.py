#!/usr/bin/env python3
"""
bump-algenta-sdk-version.py — pin every package's `algenta-sdk` dependency
to a specific version.

Used by .github/workflows/sync-on-sdk-release.yml (both its
repository_dispatch and its scheduled-nightly-check paths) to keep this
repository's packages pinned to the latest published `algenta-sdk` release,
across both PyPI (Python `pyproject.toml` files) and npm (TypeScript
`package.json` files).

This script ONLY rewrites the version string already present for the
`algenta-sdk` dependency — it never adds a new dependency, never touches any
other dependency, and never touches anything under `[tool.uv.sources]` /
`workspace:` references. It is intentionally narrow: scope creep here would
undermine the same boundary scripts/check-no-engine-dependency.py exists to
enforce (that "algenta-sdk" is a plain registry dependency, sourced from the
registry, full stop).

Usage:
    python scripts/bump-algenta-sdk-version.py --version 1.0.12 [--root .] [--dry-run]

Exit codes:
  0  ran cleanly (whether or not anything changed — see stdout for which
     files were touched)
  1  --version was not a plausible version string, or a file couldn't be
     read/written
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-.].+)?$")

# Matches e.g. `"algenta-sdk>=1.0.11"`, `'algenta-sdk >=1.0.11,<2'`,
# `"algenta-sdk==1.0.11"` inside a pyproject.toml dependency list entry.
PY_DEP_RE = re.compile(r"""(['"])algenta-sdk\s*[><=!~^]+\s*[0-9][^'"]*\1""")

# Matches e.g. `"algenta-sdk": "^1.0.11"` inside a package.json.
JSON_DEP_RE = re.compile(r'("algenta-sdk"\s*:\s*")[\^~>=]*[0-9][^"]*(")')


def bump_pyproject(path: Path, version: str, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    new_text, count = PY_DEP_RE.subn(lambda m: f"{m.group(1)}algenta-sdk>={version}{m.group(1)}", text)
    if count == 0:
        return False
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return True


def bump_package_json(path: Path, version: str, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    new_text, count = JSON_DEP_RE.subn(lambda m: f"{m.group(1)}^{version}{m.group(2)}", text)
    if count == 0:
        return False
    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return True


EXCLUDED_DIR_NAMES = {".git", "node_modules", "dist", "build", ".venv", "__pycache__"}


def iter_manifests(root: Path, name: str):
    for path in root.rglob(name):
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="New algenta-sdk version, e.g. 1.0.12")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not VERSION_RE.match(args.version):
        print(f"bump-algenta-sdk-version: '{args.version}' doesn't look like a version (expected X.Y.Z)", file=sys.stderr)
        return 1

    root = args.root.resolve()
    changed: list[Path] = []

    for pyproject in iter_manifests(root, "pyproject.toml"):
        if bump_pyproject(pyproject, args.version, args.dry_run):
            changed.append(pyproject)

    for package_json in iter_manifests(root, "package.json"):
        if bump_package_json(package_json, args.version, args.dry_run):
            changed.append(package_json)

    if not changed:
        print(f"bump-algenta-sdk-version: no files reference an algenta-sdk version pin under {root}")
        return 0

    verb = "would update" if args.dry_run else "updated"
    for path in changed:
        print(f"{verb}: {path.relative_to(root)} -> algenta-sdk {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
