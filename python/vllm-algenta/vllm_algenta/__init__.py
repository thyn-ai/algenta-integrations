"""vllm-algenta: point vLLM (or any OpenAI-client-based consumer) at Algenta's own
OpenAI-compatible `/v1/chat/completions` / `/v1/responses` surface.

See `vllm_algenta.client` for the one real thing this package provides, and this package's
README for an explicit, honest accounting of what that surface supports today (a plain chat
completion, a hardcoded `finish_reason`, synthetic rechunked streaming) versus what it does not
(tool-calling, native per-token backend streaming) -- verified directly against
`apps/api_server/schemas/llm.py` / `apps/api_server/routers/llm.py` in `thyn-ai/algenta`, not
assumed.
"""

from __future__ import annotations

from .client import (
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    DEFAULT_ENGINE_ROOT,
    build_async_client,
    build_client,
    resolve_v1_base_url,
)

__all__ = [
    "API_KEY_ENV_VAR",
    "BASE_URL_ENV_VAR",
    "DEFAULT_ENGINE_ROOT",
    "build_async_client",
    "build_client",
    "resolve_v1_base_url",
]
