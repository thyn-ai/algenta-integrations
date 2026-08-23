#!/usr/bin/env python3
"""
latest-published-algenta-sdk-version.py — ask PyPI and npm what the current
published `algenta-sdk` version is, and cross-check that they agree.

Used by .github/workflows/sync-on-sdk-release.yml's scheduled/manual path
(the repository_dispatch path already gets the version handed to it in the
event payload and doesn't need this). Both registries should agree, since
thyn-ai/algenta-sdk's own release.yml publishes both from the same tag in
the same run — a mismatch here means one registry's publish step failed or
is still propagating, which is worth surfacing rather than silently picking
one.

Prints the version to stdout on success. Talks to two public, unauthenticated
registry endpoints only (pypi.org, registry.npmjs.org) — no engine, no
private infrastructure, no credentials.

Exit codes:
  0  PyPI and npm agree; version printed to stdout
  1  network/parse error talking to a registry
  2  PyPI and npm published versions disagree (printed to stderr)
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PYPI_URL = "https://pypi.org/pypi/algenta-sdk/json"
NPM_URL = "https://registry.npmjs.org/algenta-sdk/latest"


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310 - fixed, hardcoded public registry URLs
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    try:
        pypi_version = fetch_json(PYPI_URL)["info"]["version"]
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        print(f"latest-published-algenta-sdk-version: failed to query PyPI: {exc}", file=sys.stderr)
        return 1

    try:
        npm_version = fetch_json(NPM_URL)["version"]
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        print(f"latest-published-algenta-sdk-version: failed to query npm: {exc}", file=sys.stderr)
        return 1

    if pypi_version != npm_version:
        print(
            f"latest-published-algenta-sdk-version: PyPI ({pypi_version}) and npm ({npm_version}) "
            "disagree on the latest algenta-sdk version -- one registry's publish may still be "
            "propagating. Refusing to pick one arbitrarily.",
            file=sys.stderr,
        )
        return 2

    print(pypi_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
