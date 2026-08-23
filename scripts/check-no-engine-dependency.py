#!/usr/bin/env python3
"""
check-no-engine-dependency.py — CI gate for the one rule this repo cannot break.

No package in this repository may vendor, import, or depend on Algenta's
closed-source engine (the private `decision-engine` repository: its
`mojo/` kernel tree, `apps/api_server/` or `apps/mcp_server/` server
internals) or on any local/relative filesystem path outside this repo. The
only Algenta dependency any package may declare is the published
`algenta-sdk` package (PyPI and npm) — a thin HTTP/MCP client with zero
engine source in it.

This script scans exactly the three surfaces named in the project brief:
  1. every `pyproject.toml`  (structurally, via tomllib)
  2. every `package.json`    (structurally, via json)
  3. every import/require statement in *.py / *.ts / *.tsx / *.js / *.jsx /
     *.mjs / *.cjs source files (line-oriented regex, not full parsing)

It deliberately does NOT scan prose (README/CONTRIBUTING/NOTICE/CLA/etc.),
comments, or string literals that aren't import/require targets — this repo's
own documentation legitimately discusses "decision-engine", "mojo", and
"apps/api_server" in prose to explain why they're forbidden, and flagging
that would make the check impossible to satisfy while still documenting the
constraint it enforces.

IMPORTANT — a deliberate non-symmetry: the published `algenta-sdk` PyPI
package's own Python import namespace is `decision_engine` (underscore) —
confirmed from thyn-ai/algenta-sdk's packages/python-sdk/pyproject.toml and
README (`from decision_engine import AlgentaClient`). That is a legitimate,
correct import of the published SDK and must NOT be flagged. What must be
flagged is the hyphenated string "decision-engine" (and, defensively, an
underscore-spelled dependency of the same name) appearing as a MANIFEST
DEPENDENCY NAME or SOURCE — i.e. someone trying to depend on the private
engine repository itself rather than on the published SDK package. Python
identifiers cannot contain hyphens, so "decision-engine" can only appear in
manifests/URLs/paths, never as a bare Python import — which is exactly the
surface this script locks down.

Exit codes:
  0  clean
  1  one or more violations found (see stdout for details)
  2  usage / internal error (e.g. malformed manifest)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older interpreters
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

# ── directories we never walk into ──────────────────────────────────────────
EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".turbo",
    ".pnpm-store",
}

# ── the banned vocabulary ───────────────────────────────────────────────────
# Matched case-insensitively against manifest dependency NAMES and SOURCE
# values (version strings, `file:`/`git:`/`path`/`url` fields). NOT matched
# against prose or Python import targets (see module docstring for why
# "decision_engine" the SDK import name is intentionally absent from this
# list).
BANNED_SUBSTRINGS = (
    "decision-engine",
    "decision_engine",  # defensive: catches an npm/pip name using underscore too
    "mojo",
    "apps.api_server",
    "apps/api_server",
    "apps.mcp_server",
    "apps/mcp_server",
)

# The only Algenta package this repo may ever depend on.
ALLOWED_ALGENTA_PACKAGE_NAMES = {"algenta-sdk"}

# Local-path indicators in a dependency source. A workspace-internal
# self-reference (uv `workspace = true`, pnpm `workspace:*`) is fine and is
# special-cased separately — these prefixes are never fine.
LOCAL_PATH_PREFIXES = ("file:", "link:", "../", "./")

PY_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+([A-Za-z0-9_.]+)", re.MULTILINE
)
# Recognizes `import x from "spec"`, `import "spec"`, `export ... from "spec"`,
# and `require("spec")` — single or double quoted.
JS_IMPORT_RE = re.compile(
    r"""(?:from\s+|require\(\s*)['"]([^'"]+)['"]"""
)

BANNED_PY_MODULE_PREFIXES = ("mojo", "apps.api_server", "apps.mcp_server")


@dataclass
class Violation:
    path: Path
    line: int | None
    reason: str

    def render(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root)
        except ValueError:
            rel = self.path
        loc = f"{rel}:{self.line}" if self.line else str(rel)
        return f"  {loc}: {self.reason}"


def iter_files(root: Path, names: tuple[str, ...] | None = None, suffixes: tuple[str, ...] | None = None):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        if names and path.name not in names:
            continue
        if suffixes and path.suffix not in suffixes:
            continue
        if names or suffixes:
            yield path


def _norm(s: str) -> str:
    return s.lower().strip()


def _contains_banned(s: str) -> str | None:
    low = _norm(s)
    for banned in BANNED_SUBSTRINGS:
        if banned in low:
            return banned
    return None


def _is_local_path_source(value: str) -> bool:
    v = value.strip()
    if v.startswith(LOCAL_PATH_PREFIXES):
        return True
    # A bare filesystem path (posix or windows) rather than a version
    # specifier or registry name.
    if v.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", v):
        return True
    return False


def check_dependency_pair(name: str, source: str, location: Path, line: int | None) -> list[Violation]:
    violations: list[Violation] = []
    norm_name = _norm(name)

    banned_in_name = _contains_banned(name)
    if banned_in_name:
        violations.append(
            Violation(location, line, f"dependency name '{name}' references banned target '{banned_in_name}'")
        )

    if norm_name in {"algenta", "algenta-engine", "algenta_engine"}:
        violations.append(
            Violation(location, line, f"dependency name '{name}' looks like an attempt to depend on the engine, not the published SDK ('algenta-sdk')")
        )

    if source:
        banned_in_source = _contains_banned(source)
        if banned_in_source:
            violations.append(
                Violation(location, line, f"dependency '{name}' source '{source}' references banned target '{banned_in_source}'")
            )
        elif _is_local_path_source(source):
            violations.append(
                Violation(location, line, f"dependency '{name}' resolves to a local/relative filesystem path ('{source}') — only the published registry package is allowed")
            )

    # If this dependency claims to BE the Algenta SDK (by containing
    # "algenta" in its name) it must be exactly one of the allowed published
    # names, sourced from the registry (no path/git override at all).
    if "algenta" in norm_name and norm_name not in ALLOWED_ALGENTA_PACKAGE_NAMES:
        violations.append(
            Violation(
                location,
                line,
                f"dependency '{name}' looks Algenta-related but is not the published SDK package name ({sorted(ALLOWED_ALGENTA_PACKAGE_NAMES)})",
            )
        )

    return violations


def check_pyproject(path: Path) -> list[Violation]:
    if tomllib is None:
        return [Violation(path, None, "tomllib/tomli unavailable — cannot parse pyproject.toml (Python 3.11+ required to run this check)")]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface any parse error as a finding
        return [Violation(path, None, f"failed to parse: {exc}")]

    violations: list[Violation] = []

    def dep_strings(deps: object) -> list[str]:
        if isinstance(deps, list):
            return [d for d in deps if isinstance(d, str)]
        return []

    project = data.get("project", {})
    for dep in dep_strings(project.get("dependencies")):
        pkg_name = re.split(r"[\s<>=!\[;]", dep, maxsplit=1)[0]
        violations.extend(check_dependency_pair(pkg_name, dep, path, None))

    for group, deps in (project.get("optional-dependencies") or {}).items():
        for dep in dep_strings(deps):
            pkg_name = re.split(r"[\s<>=!\[;]", dep, maxsplit=1)[0]
            violations.extend(check_dependency_pair(pkg_name, dep, path, None))

    for group, deps in (data.get("dependency-groups") or {}).items():
        for dep in dep_strings(deps):
            pkg_name = re.split(r"[\s<>=!\[;]", dep, maxsplit=1)[0]
            violations.extend(check_dependency_pair(pkg_name, dep, path, None))

    poetry_deps = (
        data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        if isinstance(data.get("tool", {}).get("poetry", {}), dict)
        else {}
    )
    for name, spec in poetry_deps.items():
        if isinstance(spec, dict):
            source = spec.get("path") or spec.get("git") or spec.get("url") or ""
            violations.extend(check_dependency_pair(name, str(source), path, None))
        elif isinstance(spec, str):
            violations.extend(check_dependency_pair(name, spec, path, None))

    # uv's dependency-source overrides: [tool.uv.sources.<name>]
    uv_sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    if isinstance(uv_sources, dict):
        for name, spec in uv_sources.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("workspace") is True:
                # Intra-repo workspace member reference — never leaves this repo.
                continue
            source = spec.get("path") or spec.get("git") or spec.get("url") or ""
            if source:
                violations.extend(check_dependency_pair(name, str(source), path, None))
                # A `path` source additionally needs to be checked for escaping
                # the repository root, even if it doesn't match a banned string.
                if spec.get("path"):
                    resolved = (path.parent / str(spec["path"])).resolve()
                    if not _within_repo(resolved, path):
                        violations.append(
                            Violation(path, None, f"tool.uv.sources.{name}.path='{spec['path']}' resolves outside this repository")
                        )

    return violations


def _within_repo(resolved: Path, manifest_path: Path) -> bool:
    # Walk up from the manifest to find the repo root (marked by .git), and
    # confirm the resolved path is inside it. Falls back to "same repo tree
    # as CHECK_ROOT" if .git isn't found (e.g. a fixture directory in tests).
    root = manifest_path.parent
    while root != root.parent and not (root / ".git").exists():
        root = root.parent
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def check_package_json(path: Path) -> list[Violation]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [Violation(path, None, f"failed to parse: {exc}")]

    violations: list[Violation] = []
    dep_fields = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
    for field in dep_fields:
        deps = data.get(field)
        if not isinstance(deps, dict):
            continue
        for name, source in deps.items():
            source_str = str(source)
            if source_str.startswith("workspace:"):
                # pnpm intra-workspace reference — never leaves this repo.
                if _contains_banned(name):
                    violations.append(
                        Violation(path, None, f"dependency name '{name}' references banned target even as a workspace: reference")
                    )
                continue
            violations.extend(check_dependency_pair(name, source_str, path, None))

    return violations


def check_python_imports(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        m = PY_IMPORT_RE.match(line)
        if not m:
            continue
        module = m.group(1)
        if any(module == prefix or module.startswith(prefix + ".") for prefix in BANNED_PY_MODULE_PREFIXES):
            violations.append(Violation(path, lineno, f"forbidden import of engine-internal module '{module}'"))
    return violations


def check_js_imports(path: Path, repo_root: Path) -> list[Violation]:
    violations: list[Violation] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in JS_IMPORT_RE.finditer(line):
            spec = m.group(1)
            banned = _contains_banned(spec)
            if banned:
                violations.append(Violation(path, lineno, f"import/require of '{spec}' references banned target '{banned}'"))
                continue
            if spec.startswith("."):
                resolved = (path.parent / spec).resolve()
                if not _within_repo(resolved, path):
                    violations.append(Violation(path, lineno, f"relative import '{spec}' resolves outside this repository"))
    return violations


def run(root: Path) -> list[Violation]:
    violations: list[Violation] = []

    for pyproject in iter_files(root, names=("pyproject.toml",)):
        violations.extend(check_pyproject(pyproject))

    for package_json in iter_files(root, names=("package.json",)):
        violations.extend(check_package_json(package_json))

    for py_file in iter_files(root, suffixes=(".py",)):
        violations.extend(check_python_imports(py_file))

    for js_file in iter_files(root, suffixes=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        violations.extend(check_js_imports(js_file, root))

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root to scan (default: cwd)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    violations = run(root)

    if not violations:
        print(f"check-no-engine-dependency: clean ({root})")
        return 0

    print(f"check-no-engine-dependency: {len(violations)} violation(s) found under {root}\n")
    for v in violations:
        print(v.render(root))
    print(
        "\nEvery package in this repository may depend only on the published "
        "'algenta-sdk' package (PyPI/npm) and the customer's own self-hosted "
        "Algenta Engine over HTTP/MCP at runtime. See CONTRIBUTING.md."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
