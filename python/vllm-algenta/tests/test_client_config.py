"""Fast, no-network unit tests for `vllm_algenta.client`'s pure configuration-resolution logic."""

from __future__ import annotations

import pytest
from vllm_algenta.client import (
    API_KEY_ENV_VAR,
    BASE_URL_ENV_VAR,
    DEFAULT_ENGINE_ROOT,
    build_client,
    resolve_v1_base_url,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


def test_resolve_v1_base_url_defaults_to_localhost_never_algenta_hosted() -> None:
    assert resolve_v1_base_url() == f"{DEFAULT_ENGINE_ROOT}/v1"
    assert "algenta.ai" not in resolve_v1_base_url()


def test_resolve_v1_base_url_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://my-engine.internal:9000")
    assert resolve_v1_base_url() == "http://my-engine.internal:9000/v1"


def test_resolve_v1_base_url_explicit_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://from-env:9000")
    assert resolve_v1_base_url("http://from-arg:9000") == "http://from-arg:9000/v1"


def test_resolve_v1_base_url_does_not_double_a_trailing_v1() -> None:
    assert resolve_v1_base_url("http://my-engine.internal:9000/v1") == "http://my-engine.internal:9000/v1"


def test_resolve_v1_base_url_strips_trailing_slash() -> None:
    assert resolve_v1_base_url("http://my-engine.internal:9000/") == "http://my-engine.internal:9000/v1"


def test_build_client_raises_a_clear_error_with_no_api_key_configured_anywhere() -> None:
    with pytest.raises(RuntimeError, match=API_KEY_ENV_VAR):
        build_client(base_url="http://localhost:8000")


def test_build_client_accepts_an_explicit_api_key() -> None:
    client = build_client(base_url="http://localhost:8000", api_key="sk-test")
    assert client.api_key == "sk-test"
    assert str(client.base_url).rstrip("/") == "http://localhost:8000/v1"


def test_build_client_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, "sk-from-env")
    client = build_client(base_url="http://localhost:8000")
    assert client.api_key == "sk-from-env"
