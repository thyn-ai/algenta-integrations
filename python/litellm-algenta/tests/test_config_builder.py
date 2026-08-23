"""Unit tests for `litellm_algenta.config` -- no subprocess, no network, no litellm import.

These exercise the pure-Python generation/lint/merge logic directly. The claims *about LiteLLM's
own gateway behavior* that this logic is based on (that `allowed_tools`/`allowed_params` are real
call-time gates, that `os.environ/VAR` resolves, that `oauth2` without `oauth2_flow` crashes
startup, ...) are re-verified against a real running proxy in `test_gateway_conformance.py` --
this file only checks that this package's own generation/lint code does what it claims to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from litellm_algenta.config import (
    ConfigError,
    build_mcp_server_entry,
    lint_mcp_server_entry,
    merge_into_config_file,
    render_yaml,
)
from litellm_algenta.contract import (
    EXECUTE_DECISION,
    EXECUTE_DECISION_MODEL_FACING_PARAMS,
    NEVER_MODEL_FACING_FIELDS,
    TOOL_PROFILES,
)


@pytest.mark.parametrize("profile", ["observe", "govern", "execute", "full"])
def test_allowed_tools_matches_profile_exactly(profile: str) -> None:
    fragment = build_mcp_server_entry(profile=profile, server_name="algenta")
    entry = fragment["mcp_servers"]["algenta"]
    if profile == "full":
        # "full" means "omit allowed_tools entirely" -- LiteLLM's own meaning of unrestricted.
        assert "allowed_tools" not in entry
    else:
        assert set(entry["allowed_tools"]) == TOOL_PROFILES[profile]


@pytest.mark.parametrize("profile", ["execute", "full"])
def test_execute_decision_reachable_profiles_get_allowed_params_scrub(profile: str) -> None:
    fragment = build_mcp_server_entry(profile=profile, server_name="algenta")
    entry = fragment["mcp_servers"]["algenta"]
    execute_params = set(entry["allowed_params"][EXECUTE_DECISION])
    assert execute_params == EXECUTE_DECISION_MODEL_FACING_PARAMS
    assert not (execute_params & NEVER_MODEL_FACING_FIELDS)


@pytest.mark.parametrize("profile", ["observe", "govern"])
def test_execute_decision_unreachable_profiles_have_no_allowed_params_entry(profile: str) -> None:
    fragment = build_mcp_server_entry(profile=profile, server_name="algenta")
    entry = fragment["mcp_servers"]["algenta"]
    # execute_decision isn't even in allowed_tools, so there's nothing for allowed_params to
    # restrict -- and the tool is unreachable via allowed_tools alone regardless.
    assert EXECUTE_DECISION not in entry["allowed_tools"]
    assert EXECUTE_DECISION not in entry.get("allowed_params", {})


def test_url_and_token_are_env_var_placeholders_by_default() -> None:
    fragment = build_mcp_server_entry(server_name="algenta")
    entry = fragment["mcp_servers"]["algenta"]
    assert entry["url"].startswith("os.environ/")
    assert entry["authentication_token"].startswith("os.environ/")
    # Never resolves the literal secret/URL value itself -- only the placeholder.
    assert "algenta.ai" not in entry["url"]


def test_explicit_base_url_is_used_verbatim() -> None:
    fragment = build_mcp_server_entry(server_name="algenta", base_url="http://127.0.0.1:9999/mcp")
    assert fragment["mcp_servers"]["algenta"]["url"] == "http://127.0.0.1:9999/mcp"


def test_oauth2_without_flow_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="oauth2_flow"):
        build_mcp_server_entry(auth_type="oauth2")


def test_oauth2_with_flow_sets_it_and_omits_authentication_token() -> None:
    fragment = build_mcp_server_entry(auth_type="oauth2", oauth2_flow="client_credentials")
    entry = fragment["mcp_servers"][next(iter(fragment["mcp_servers"]))]
    assert entry["oauth2_flow"] == "client_credentials"
    assert "authentication_token" not in entry


def test_extra_server_fields_collision_raises() -> None:
    with pytest.raises(ConfigError, match="collides"):
        build_mcp_server_entry(extra_server_fields={"transport": "sse"})


def test_extra_server_fields_are_merged_in() -> None:
    fragment = build_mcp_server_entry(
        auth_type="oauth2",
        oauth2_flow="client_credentials",
        extra_server_fields={"token_url": "os.environ/TOKEN_URL", "client_id": "os.environ/CLIENT_ID"},
    )
    entry = fragment["mcp_servers"][next(iter(fragment["mcp_servers"]))]
    assert entry["token_url"] == "os.environ/TOKEN_URL"
    assert entry["client_id"] == "os.environ/CLIENT_ID"


def test_unknown_profile_raises() -> None:
    with pytest.raises(ConfigError, match="unknown profile"):
        build_mcp_server_entry(profile="admin")  # type: ignore[arg-type]


def test_stdio_transport_raises_instead_of_emitting_a_meaningless_url() -> None:
    # transport="stdio" is excluded from the type annotation, but Python doesn't enforce
    # Literal types at runtime -- a caller who bypasses type checking (or plain string input from
    # a config file) must not silently get back a config with a "url" key that means nothing to
    # LiteLLM's real stdio transport (which needs command/args/env instead).
    with pytest.raises(ConfigError, match="stdio"):
        build_mcp_server_entry(transport="stdio")  # type: ignore[arg-type]


def test_lint_flags_stdio_transport_with_a_url_key() -> None:
    fragment = {
        "mcp_servers": {
            "x": {"url": "os.environ/ALGENTA_MCP_URL", "transport": "stdio", "allowed_tools": []}
        }
    }
    violations = lint_mcp_server_entry(fragment)
    assert any("stdio" in v and "url" in v for v in violations)


def test_render_yaml_round_trips_through_yaml_parser() -> None:
    fragment = build_mcp_server_entry(profile="execute", server_name="algenta")
    rendered = render_yaml(fragment)
    parsed = yaml.safe_load(rendered)
    assert parsed["mcp_servers"]["algenta"] == fragment["mcp_servers"]["algenta"]


def test_render_yaml_header_lists_every_env_var_actually_referenced() -> None:
    fragment = build_mcp_server_entry(server_name="algenta")
    rendered = render_yaml(fragment)
    header, _, _ = rendered.partition("mcp_servers:")
    assert "ALGENTA_MCP_URL" in header
    assert "ALGENTA_MCP_TOKEN" in header


def test_render_yaml_without_header() -> None:
    fragment = build_mcp_server_entry(server_name="algenta")
    rendered = render_yaml(fragment, with_header=False)
    assert not rendered.startswith("#")
    assert yaml.safe_load(rendered) == fragment


# --- lint_mcp_server_entry / assert_safe -----------------------------------------------------


def test_lint_accepts_every_profile_this_module_builds() -> None:
    for profile in TOOL_PROFILES:
        fragment = build_mcp_server_entry(profile=profile, server_name="algenta")
        assert lint_mcp_server_entry(fragment) == []


def test_lint_flags_execute_decision_reachable_with_no_allowed_params() -> None:
    fragment = {"mcp_servers": {"x": {"url": "http://localhost:8000/mcp", "allowed_tools": [EXECUTE_DECISION]}}}
    violations = lint_mcp_server_entry(fragment)
    assert any("allowed_params" in v for v in violations)


def test_lint_flags_never_model_facing_field_leaked_into_allowed_params() -> None:
    fragment = {
        "mcp_servers": {
            "x": {
                "url": "http://localhost:8000/mcp",
                "allowed_tools": [EXECUTE_DECISION],
                "allowed_params": {EXECUTE_DECISION: ["plan_hash", "force"]},
            }
        }
    }
    violations = lint_mcp_server_entry(fragment)
    assert any("never-model-facing" in v for v in violations)


def test_lint_flags_algenta_hosted_url() -> None:
    fragment = {"mcp_servers": {"x": {"url": "https://api.algenta.ai/mcp", "allowed_tools": []}}}
    violations = lint_mcp_server_entry(fragment)
    assert any("Algenta-hosted" in v for v in violations)


def test_lint_flags_oauth2_without_flow() -> None:
    fragment = {"mcp_servers": {"x": {"url": "http://localhost:8000/mcp", "auth_type": "oauth2"}}}
    violations = lint_mcp_server_entry(fragment)
    assert any("oauth2_flow" in v for v in violations)


def test_assert_safe_raises_on_any_violation() -> None:
    fragment = {"mcp_servers": {"x": {"url": "https://api.algenta.ai/mcp"}}}
    from litellm_algenta.config import assert_safe

    with pytest.raises(ConfigError):
        assert_safe(fragment)


# --- merge_into_config_file --------------------------------------------------------------------


def test_merge_creates_new_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    fragment = build_mcp_server_entry(server_name="algenta")
    merged = merge_into_config_file(config_path, fragment)
    assert merged["mcp_servers"]["algenta"] == fragment["mcp_servers"]["algenta"]
    assert yaml.safe_load(config_path.read_text()) == merged


def test_merge_preserves_unrelated_existing_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"model_list": [{"model_name": "gpt-4o-mini"}], "mcp_servers": {"other": {"url": "x"}}})
    )
    fragment = build_mcp_server_entry(server_name="algenta")
    merged = merge_into_config_file(config_path, fragment)
    assert merged["model_list"] == [{"model_name": "gpt-4o-mini"}]
    assert merged["mcp_servers"]["other"] == {"url": "x"}
    assert merged["mcp_servers"]["algenta"] == fragment["mcp_servers"]["algenta"]


def test_merge_is_idempotent_on_the_same_server_name(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    merge_into_config_file(config_path, build_mcp_server_entry(server_name="algenta", profile="observe"))
    merged = merge_into_config_file(config_path, build_mcp_server_entry(server_name="algenta", profile="execute"))
    # Re-running with the same server_name overwrites in place -- no duplicate key, no leftover
    # from the first (narrower) profile's allowed_tools.
    assert len(merged["mcp_servers"]) == 1
    assert EXECUTE_DECISION in merged["mcp_servers"]["algenta"]["allowed_tools"]
