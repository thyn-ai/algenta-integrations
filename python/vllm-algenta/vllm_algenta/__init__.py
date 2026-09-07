"""vllm-algenta: point vLLM (or any OpenAI-client-based consumer) at Algenta's own
OpenAI-compatible `/v1/chat/completions` / `/v1/responses` surface.

See `vllm_algenta.client` for the one real thing this package provides, and this package's
README for an explicit, honest, model-dependent accounting of what that surface supports today --
tool calling and a widened `finish_reason` are real capabilities of a configured provider-backed
or bundled `algenta_local` model, while this package's own zero-config default model
(`text.tokenizer`) remains a deterministic utility model with neither -- verified directly against
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
