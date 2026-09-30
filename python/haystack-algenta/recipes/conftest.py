"""Pytest fixtures shared by the recipe tests.

The stub-server fixture lives in `tests/conftest.py` so the existing package suite can reuse it.
Recipes are a separate directory, so we re-export it here to keep each test file self-contained.
"""

from __future__ import annotations

from tests.conftest import stub_server

__all__ = ["stub_server"]
