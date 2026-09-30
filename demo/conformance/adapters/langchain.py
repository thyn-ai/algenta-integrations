"""LangChain adapter for the conformance suite.

Every engine request is routed through a LangChain `StructuredTool` so the suite exercises the
framework's tool-invocation machinery, not just a direct HTTP client. The tool itself performs the
same authenticated HTTP calls the direct adapter does; the value is in proving the semantics survive
the LangChain layer.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from . import Adapter


def _maybe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return None


class _HttpRequestInput(BaseModel):
    method: str = Field(description="HTTP method for the request.")
    path: str = Field(description="URL path, relative to the engine base URL.")
    body_json: str = Field(default="{}", description="JSON-serialized request body, or '{}'.")
    header_json: str = Field(default="{}", description="JSON-serialized extra headers, or '{}'.")


class LangChainAdapter(Adapter):
    """Conformance adapter that routes every engine request through a LangChain `StructuredTool`.

    The tool is intentionally generic (one tool handles every path/method) so the suite's scenario
    implementations can stay unchanged and the adapter stays thin. The tool's coroutine performs a
    real synchronous `httpx` request to the caller's self-hosted engine and returns the full
    response envelope -- status, parsed body, headers, and raw bytes -- so assertions remain
    against the actual HTTP response.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.client = httpx.Client(timeout=60)
        self._tool = self._build_tool()

    def _build_tool(self) -> StructuredTool:
        def _make_request(
            method: str, path: str, body_json: str, header_json: str
        ) -> dict[str, Any]:
            body = json.loads(body_json) if body_json else None
            extra_headers = json.loads(header_json) if header_json else {}
            response = self.client.request(
                method=method,
                url=f"{self.base}{path}",
                json=body,
                headers={
                    "X-API-Key": self.key,
                    "Content-Type": "application/json",
                    **extra_headers,
                },
            )
            raw = response.content
            return {
                "status": response.status_code,
                "body": _maybe_json(raw),
                "headers": dict(response.headers),
                "raw": raw,
            }

        return StructuredTool.from_function(
            name="algenta_http_request",
            description="Make one authenticated HTTP request to the self-hosted Algenta engine.",
            args_schema=_HttpRequestInput,
            func=_make_request,
        )

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None, dict[str, str], bytes]:
        result = self._tool.invoke(
            {
                "method": method,
                "path": path,
                "body_json": json.dumps(body) if body is not None else "{}",
                "header_json": json.dumps(headers) if headers else "{}",
            }
        )
        return result["status"], result["body"], result["headers"], result["raw"]

    def code_of(self, body: Any) -> str:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("code", ""))
            detail = body.get("detail")
            if isinstance(detail, dict):
                return str(detail.get("code", ""))
        return ""
