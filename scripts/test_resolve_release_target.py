#!/usr/bin/env python3
"""
Regression test for resolve_release_target.py.

The interesting cases are the REFUSALS, not the happy path. A resolver that always answers
something is worse than no resolver: it would let a stale tag publish whatever happens to be on
disk under a version number those artifacts do not carry, and a bad upload to PyPI cannot be
replaced -- only yanked.

Run directly:  python3 scripts/test_resolve_release_target.py
Also wired into .github/workflows/ci.yml.

Stdlib only, same as test_compute_release_bumps.py and test_check_no_engine_dependency.py -- the
CI job that runs these does not install pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from compute_release_bumps import Package, discover_packages  # noqa: E402
from resolve_release_target import REGISTRY_FOR_KIND, ResolutionError, resolve  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL: {message}")


def expect_refusal(tag: str, packages: list[Package], code: int, must_mention: str) -> None:
    try:
        target = resolve(tag, packages)
    except ResolutionError as exc:
        check(exc.code == code, f"{tag}: expected exit code {code}, got {exc.code}")
        check(
            must_mention in str(exc),
            f"{tag}: error should mention {must_mention!r}, said: {exc}",
        )
        return
    FAILURES.append(f"{tag}: resolved to {target.name} instead of being refused")
    print(f"  FAIL: {tag} resolved instead of being refused")


def _pkg(name: str, version: str, kind: str = "python", subdir: str = "python") -> Package:
    directory = REPO_ROOT / subdir / name
    return Package(
        name=name,
        dir=directory,
        version_file=directory / "pyproject.toml",
        kind=kind,
        current_version=version,
    )


def expect_resolution(tag: str, packages: list[Package], expected_name: str) -> None:
    """Resolve, or record a clean failure. Never let a ResolutionError escape as a traceback.

    A bare `resolve()` in a test body turns a wrong refusal into a crash that aborts the whole run
    before the later tests execute -- so one bug hides every bug after it.
    """
    try:
        target = resolve(tag, packages)
    except ResolutionError as exc:
        FAILURES.append(f"{tag}: expected {expected_name}, was refused: {exc}")
        print(f"  FAIL: {tag} was refused: {exc}")
        return
    check(target.name == expected_name, f"{tag}: resolved to {target.name}, want {expected_name}")


def test_resolves_a_matching_python_tag() -> None:
    target = resolve("widget-algenta-v1.2.3", [_pkg("widget-algenta", "1.2.3")])
    check(target.name == "widget-algenta", f"name was {target.name}")
    check(target.version == "1.2.3", f"version was {target.version}")
    check(target.registry == "pypi", f"registry was {target.registry}")
    check(target.directory == "python/widget-algenta", f"directory was {target.directory}")


def test_a_node_package_resolves_to_npm() -> None:
    target = resolve(
        "algenta-tools-v0.1.1",
        [_pkg("algenta-tools", "0.1.1", kind="node", subdir="typescript")],
    )
    check(target.registry == "npm", f"registry was {target.registry}")


def test_a_version_mismatch_is_refused_not_published() -> None:
    """The whole point. A tag can outlive the commit that produced it."""
    expect_refusal("widget-algenta-v9.9.9", [_pkg("widget-algenta", "1.2.3")], 3, "1.2.3")


def test_an_unknown_tag_is_refused_and_names_what_it_knows() -> None:
    expect_refusal("not-a-package-v1.0.0", [_pkg("widget-algenta", "1.2.3")], 2, "widget-algenta")


def test_an_empty_package_set_is_refused_rather_than_silently_resolving() -> None:
    expect_refusal("widget-algenta-v1.2.3", [], 2, "none discovered")


def test_matching_is_by_exact_name_not_by_splitting_on_the_last_dash_v() -> None:
    """Package names contain hyphens, so a positional split is a guess where a match is available.

    One name can also be a PREFIX of another. This caught a real bug: with `srv-algenta` checked
    first, `srv-algenta-v2-v3.0.0` bound to it and then failed as a "version mismatch" that was
    really a misidentification. Longest name wins, same rule as a routing table.
    """
    packages = [_pkg("srv-algenta", "0.1.0"), _pkg("srv-algenta-v2", "3.0.0")]
    expect_resolution("srv-algenta-v2-v3.0.0", packages, "srv-algenta-v2")
    expect_resolution("srv-algenta-v0.1.0", packages, "srv-algenta")
    # Order in the list must not decide it either.
    expect_resolution("srv-algenta-v2-v3.0.0", list(reversed(packages)), "srv-algenta-v2")


# ── against the real repository ──────────────────────────────────────────────────────────────────


def test_every_discovered_package_kind_maps_to_a_registry() -> None:
    """A new package kind must not silently become unpublishable."""
    kinds = {p.kind for p in discover_packages()}
    check(bool(kinds), "no packages discovered -- the discovery globs are wrong")
    unmapped = kinds - set(REGISTRY_FOR_KIND)
    check(unmapped == set(), f"package kinds with no registry: {unmapped}")


def test_the_real_packages_resolve_from_their_own_current_tags() -> None:
    """Every real package's tag-at-its-current-version resolves to a real directory.

    Catches a change to the tag format in compute_release_bumps.py that would leave publish.yml
    unable to identify anything -- the two must agree on the format, and only one of them writes it.
    """
    packages = discover_packages()
    check(bool(packages), "no packages discovered")
    for package in packages:
        tag = f"{package.name}-v{package.current_version}"
        try:
            target = resolve(tag)
        except ResolutionError as exc:
            FAILURES.append(f"{tag} did not resolve: {exc}")
            print(f"  FAIL: {tag} did not resolve: {exc}")
            continue
        check(target.name == package.name, f"{tag} resolved to {target.name}")
        check((REPO_ROOT / target.directory).is_dir(), f"{target.directory} is not a directory")


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
