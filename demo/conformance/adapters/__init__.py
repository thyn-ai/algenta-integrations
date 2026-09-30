"""Adapter protocol for the cross-framework conformance suite.

The suite in `demo.conformance.runner` is written against this small, explicit interface so the
same 12 scenarios can be exercised through a direct stdlib HTTP client (`Engine`, kept in
`runner.py`) or through a framework adapter such as the LangChain adapter in
`demo.conformance.adapters.langchain`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Adapter(Protocol):
    """Minimal interface a conformance-suite adapter must implement.

    The return shape mirrors the existing direct HTTP client: status code, optional parsed JSON
    body, response headers, and the raw response bytes. Keeping the interface this thin means the
    scenario implementations in `runner.py` do not need to know which framework is underneath.
    """

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None, dict[str, str], bytes]:
        """Make one authenticated HTTP request and return the full response envelope."""
        ...

    def code_of(self, body: Any) -> str:
        """Extract a named error code from a parsed response body, if one is present."""
        ...
