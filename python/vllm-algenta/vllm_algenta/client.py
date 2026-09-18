"""vllm_algenta.client -- point the standard `openai` Python client at Algenta's own
OpenAI-compatible `/v1/chat/completions` / `/v1/responses` surface.

Direction, stated plainly: this package is vLLM (or any other OpenAI-client-based consumer)
pointed AT Algenta's own OpenAI-compatible HTTP API -- never the other direction (Algenta calling
out to a vLLM-hosted model). That reverse direction isn't buildable in this repository at all --
`scripts/check-no-engine-dependency.py` forbids depending on or reimplementing engine internals,
and every other package here is already shaped as "framework talks to Algenta," never the
reverse. See this package's README for the full reasoning.

Genuinely all a vLLM-ecosystem (or any OpenAI-SDK-based) consumer needs is the standard `openai`
client pointed at the right `base_url` -- confirmed directly, not assumed: a real
`openai.OpenAI().chat.completions.create(...)` call, both streaming and non-streaming, correctly
parses a response shaped exactly like Algenta's real `ChatCompletionsResponse`
(`apps/api_server/schemas/llm.py` in `thyn-ai/algenta`) even though that response omits the
`created` field the OpenAI wire format normally includes (the SDK's own model treats it as
optional and leaves it `None` rather than raising) -- see `tests/test_chat_completions_matrix.py`
for the reproduction. This module exists only to save a caller from hand-computing the right
`base_url` and remembering the right env var name; it adds no request/response shaping of its own.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import AsyncOpenAI, OpenAI

#: Resolved in the same order, and with the same self-hosted-first intent, as every other package
#: in this repository -- but note the VALUE this package expects differs from some MCP-based
#: siblings' own `ALGENTA_BASE_URL` default. `pydantic_ai_algenta.toolset
#: .DEFAULT_ALGENTA_BASE_URL`, for example, bakes the `/mcp` suffix directly into its own fallback
#: constant (`"http://localhost:8000/mcp"`), because that whole string is what gets handed
#: straight to an `MCPToolset`. This package instead treats `ALGENTA_BASE_URL` as the engine's
#: bare HTTP origin (e.g. `http://localhost:8000`, no path suffix) and appends `/v1` itself --
#: matching the root README's own framing of `ALGENTA_BASE_URL` as "your own deployment" in
#: general, with each package then appending whatever surface-specific path it actually talks to.
#: Do not copy an MCP sibling's `/mcp`-suffixed value verbatim into this package's environment;
#: `resolve_v1_base_url` below tolerates a stray trailing `/v1` (so re-exporting the same env var
#: with `/v1` already appended is harmless) but has no way to know a `/mcp` suffix wasn't meant
#: for it.
BASE_URL_ENV_VAR = "ALGENTA_BASE_URL"
DEFAULT_ENGINE_ROOT = "http://localhost:8000"

#: Every route on Algenta's LLM API router carries `Depends(require_verified_email)` and
#: `Depends(bind_tenant_keys)` (`apps/api_server/routers/llm.py` in `thyn-ai/algenta`, confirmed
#: by reading the router's own `APIRouter(... dependencies=[...])` declaration) -- i.e. every
#: request needs an authenticated, org-bound caller identity, in every deployment mode this
#: package has been able to verify from source. `build_client`/`build_async_client` below treat a
#: credential as required in practice: they raise a clear error rather than silently sending a
#: placeholder string that would just 401 deep inside the `openai` SDK's own error handling.
API_KEY_ENV_VAR = "ALGENTA_API_KEY"


def resolve_v1_base_url(base_url: str | None = None) -> str:
    """Resolve the `{engine_root}/v1` URL `build_client`/`build_async_client` point the `openai`
    client at.

    Args:
        base_url: An explicit engine root or full `/v1` URL, overriding `ALGENTA_BASE_URL`.

    Resolution order: `base_url` (if given) -> the `ALGENTA_BASE_URL` environment variable ->
    `DEFAULT_ENGINE_ROOT` (`http://localhost:8000`, self-hosted-first -- never an Algenta-hosted
    default, matching every other package in this repository). A trailing `/v1` already present
    (from either source) is not doubled.
    """
    root = (base_url or os.environ.get(BASE_URL_ENV_VAR) or DEFAULT_ENGINE_ROOT).rstrip("/")
    if root.endswith("/v1"):
        return root
    return f"{root}/v1"


def _resolve_api_key(api_key: str | None) -> str:
    resolved = api_key or os.environ.get(API_KEY_ENV_VAR)
    if not resolved:
        raise RuntimeError(
            f"No Algenta API key configured -- pass api_key= explicitly or set the "
            f"{API_KEY_ENV_VAR} environment variable. Every route on Algenta's LLM API "
            "(apps/api_server/routers/llm.py's `require_verified_email` / `bind_tenant_keys` "
            "dependencies, in every deployment mode verified from source) needs an authenticated, "
            "org-bound caller identity; this package does not send a placeholder credential in "
            "its place, since that would only fail later with a confusing 401 instead of a clear "
            "error now."
        )
    return resolved


def build_client(*, base_url: str | None = None, api_key: str | None = None, **client_kwargs: Any) -> OpenAI:
    """Build a real `openai.OpenAI` client pointed at your own self-hosted Algenta engine.

    Args:
        base_url: See `resolve_v1_base_url`.
        api_key: Your Algenta API key/token. Falls back to the `ALGENTA_API_KEY` environment
            variable; raises `RuntimeError` if neither is set (see `API_KEY_ENV_VAR`'s docstring
            for why this package treats a credential as required rather than optional).
        **client_kwargs: Forwarded as-is to `openai.OpenAI(...)` (e.g. `timeout=`, `max_retries=`,
            `http_client=` for a pre-configured `httpx.Client`).

    Returns:
        A real, unmodified `openai.OpenAI` client -- this function does not subclass or wrap it.
        Every method (`.chat.completions.create(...)`, `.models.list()`, ...) behaves exactly as
        the `openai` package documents; whether a given method's *server-side* behavior matches
        what OpenAI itself would do is a question about Algenta's engine, not about this client,
        and this package's README documents that gap plainly for the two routes it actually tests.

    Raises:
        ModuleNotFoundError: `openai` is not installed (`pip install openai`, or this package's
            own `openai` extra).
    """
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by test_missing_openai_dependency
        raise ModuleNotFoundError(
            "vllm_algenta.client.build_client needs the `openai` package installed -- "
            "pip install openai (or pip install 'vllm-algenta[openai]')."
        ) from exc

    return OpenAI(base_url=resolve_v1_base_url(base_url), api_key=_resolve_api_key(api_key), **client_kwargs)


def build_async_client(
    *, base_url: str | None = None, api_key: str | None = None, **client_kwargs: Any
) -> AsyncOpenAI:
    """Async counterpart of `build_client`, returning a real `openai.AsyncOpenAI` client. See
    `build_client` for argument documentation -- identical in every respect but sync vs async."""
    try:
        from openai import AsyncOpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by test_missing_openai_dependency
        raise ModuleNotFoundError(
            "vllm_algenta.client.build_async_client needs the `openai` package installed -- "
            "pip install openai (or pip install 'vllm-algenta[openai]')."
        ) from exc

    return AsyncOpenAI(base_url=resolve_v1_base_url(base_url), api_key=_resolve_api_key(api_key), **client_kwargs)


__all__ = [
    "API_KEY_ENV_VAR",
    "BASE_URL_ENV_VAR",
    "DEFAULT_ENGINE_ROOT",
    "build_async_client",
    "build_client",
    "resolve_v1_base_url",
]
