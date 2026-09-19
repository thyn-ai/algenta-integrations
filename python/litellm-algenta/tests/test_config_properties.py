"""Property-based tests for `litellm_algenta.config` and `litellm_algenta.contract` (Hypothesis).

`test_config_builder.py` pins down specific, verified-real shapes with hand-picked examples. This
module states the invariants that must hold for *every* input the builder accepts and lets
Hypothesis search for a counterexample -- still no subprocess, no network, no `litellm` import.

1. Whatever `build_mcp_server_entry` produces, `lint_mcp_server_entry` accepts. The linter is the
   last line of defence for hand-written configs; the builder must clear it by construction, for
   any profile / auth type / transport / extra field a caller can legitimately pass.
2. The builder is a faithful mapper: every input lands exactly where LiteLLM reads it, and no
   other key appears in the emitted entry.
3. The `force`/`override_safety` scrub (`allowed_params.execute_decision`) is present exactly
   where `execute_decision` is reachable, is exactly the contract's model-facing parameter set,
   and the linter catches every way of weakening it (adding a never-model-facing field, or
   dropping the scrub entirely).
4. `render_yaml` is a faithful serializer: `yaml.safe_load(render_yaml(fragment))` gives the
   fragment back, and its header names exactly the `os.environ/` placeholders the fragment
   references -- no more, no fewer.
5. `merge_into_config_file` is idempotent and preserves every unrelated top-level key and every
   unrelated sibling server entry.
6. `resolve_profile_tool_names` is a filter (never invents a tool), is monotone across the four
   profiles, and exposes `execute_decision` only under `execute`/`full`.
"""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from litellm_algenta.config import (
    AuthType,
    ConfigError,
    build_mcp_server_entry,
    lint_mcp_server_entry,
    merge_into_config_file,
    render_yaml,
)
from litellm_algenta.contract import (
    EXECUTE_DECISION,
    EXECUTE_DECISION_MODEL_FACING_PARAMS,
    FULL_PROFILE_SENTINEL,
    NEVER_MODEL_FACING_FIELDS,
    TOOL_PROFILES,
    resolve_profile_tool_names,
)

# --- strategies ---------------------------------------------------------------------------------

_PROFILES: tuple[str, ...] = tuple(sorted(TOOL_PROFILES))
_AUTH_TYPES: tuple[str, ...] = get_args(AuthType)
_STATIC_CREDENTIAL_AUTH_TYPES = frozenset({"bearer_token", "api_key", "basic", "token"})
_OAUTH2_FLOWS: tuple[str, ...] = ("client_credentials", "authorization_code")
_TRANSPORTS: tuple[str, ...] = ("http", "sse")
# Every key `build_mcp_server_entry` may set itself. It refuses an `extra_server_fields` key that
# collides with one of these, so the extra-field strategy draws its keys from outside the set.
_BUILDER_MANAGED_KEYS = frozenset(
    {
        "url",
        "transport",
        "description",
        "auth_type",
        "oauth2_flow",
        "authentication_token",
        "allowed_tools",
        "allowed_params",
        "extra_headers",
    }
)

# Arbitrary text, YAML-significant characters included. Only surrogate code points (category Cs)
# are excluded: they are not UTF-8-encodable, so `merge_into_config_file`'s
# `write_text(encoding="utf-8")` could never round-trip them -- and no real config carries them.
_TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)))
_NON_EMPTY_TEXT = st.text(alphabet=st.characters(exclude_categories=("Cs",)), min_size=1)
_ENV_VAR_NAME = st.from_regex(r"\A[A-Z_][A-Z0-9_]{0,30}\Z")
_HEADER_NAME = st.from_regex(r"\A[a-z][a-z0-9-]{0,30}\Z")
# A literal self-hosted URL. The linter rejects anything that looks Algenta-hosted, so that one
# marker is filtered out here: the property under test is "the builder's output lints clean for
# every *legitimate* input", not "the linter is lenient".
_SELF_HOSTED_URL = st.from_regex(
    r"\Ahttps?://[a-z][a-z0-9-]{0,15}(\.[a-z][a-z0-9-]{0,10}){0,3}(:[1-9][0-9]{1,4})?(/[a-z0-9_-]{0,10}){0,3}\Z"
).filter(lambda url: "algenta.ai" not in url)
_YAML_SCALAR = st.none() | st.booleans() | st.integers() | _TEXT
_YAML_VALUE = (
    _YAML_SCALAR
    | st.lists(_YAML_SCALAR, max_size=3)
    | st.dictionaries(_TEXT, _YAML_SCALAR, max_size=3)
)
_EXTRA_SERVER_FIELDS = st.none() | st.dictionaries(
    _TEXT.filter(lambda key: key not in _BUILDER_MANAGED_KEYS), _YAML_VALUE, max_size=4
)


@st.composite
def builder_kwargs(draw: st.DrawFn) -> dict[str, Any]:
    """Every keyword `build_mcp_server_entry` accepts, drawn so the call is *valid* by construction.

    The one coupling the builder enforces is `auth_type="oauth2"` requiring a real `oauth2_flow`;
    for every other auth type the flow is drawn anyway (possibly `None`) so the "ignored unless
    oauth2" claim is itself under test.
    """
    auth_type = draw(st.sampled_from(_AUTH_TYPES))
    if auth_type == "oauth2":
        oauth2_flow: str | None = draw(st.sampled_from(_OAUTH2_FLOWS))
    else:
        oauth2_flow = draw(st.none() | st.sampled_from(_OAUTH2_FLOWS))
    return {
        "profile": draw(st.sampled_from(_PROFILES)),
        "server_name": draw(_NON_EMPTY_TEXT),
        "base_url_env_var": draw(_ENV_VAR_NAME),
        "base_url": draw(st.none() | _SELF_HOSTED_URL),
        "transport": draw(st.sampled_from(_TRANSPORTS)),
        "description": draw(st.none() | _TEXT),
        "auth_type": auth_type,
        "authentication_token_env_var": draw(st.none() | _ENV_VAR_NAME),
        "oauth2_flow": oauth2_flow,
        "extra_headers": draw(st.lists(_HEADER_NAME, max_size=4)),
        "extra_server_fields": draw(_EXTRA_SERVER_FIELDS),
    }


def _referenced_env_vars(value: Any) -> set[str]:
    """Independent oracle for the `os.environ/NAME` placeholders a fragment references (values only,
    never keys -- the same rule `render_yaml`'s header generator follows)."""
    if isinstance(value, str):
        return {value.removeprefix("os.environ/")} if value.startswith("os.environ/") else set()
    if isinstance(value, dict):
        return set().union(*(_referenced_env_vars(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_referenced_env_vars(v) for v in value))
    return set()


def _the_entry(fragment: dict[str, Any], server_name: str) -> dict[str, Any]:
    assert set(fragment) == {"mcp_servers"}
    assert set(fragment["mcp_servers"]) == {server_name}
    return fragment["mcp_servers"][server_name]


# --- 1 + 2: the builder is a faithful mapper whose output always lints clean --------------------


@given(kwargs=builder_kwargs())
def test_builder_maps_every_input_to_exactly_one_key_and_always_lints_clean(
    kwargs: dict[str, Any],
) -> None:
    fragment = build_mcp_server_entry(**kwargs)
    entry = _the_entry(fragment, kwargs["server_name"])

    assert lint_mcp_server_entry(fragment) == []

    # Every emitted key is accounted for by exactly one input -- nothing extra, nothing missing.
    expected_keys = {"url", "transport", "auth_type"}
    if kwargs["description"]:
        expected_keys.add("description")
    if kwargs["auth_type"] == "oauth2":
        expected_keys.add("oauth2_flow")
    elif (
        kwargs["auth_type"] in _STATIC_CREDENTIAL_AUTH_TYPES
        and kwargs["authentication_token_env_var"]
    ):
        expected_keys.add("authentication_token")
    if kwargs["profile"] != "full":
        expected_keys.add("allowed_tools")
    if (
        TOOL_PROFILES[kwargs["profile"]] == FULL_PROFILE_SENTINEL
        or EXECUTE_DECISION in TOOL_PROFILES[kwargs["profile"]]
    ):
        expected_keys.add("allowed_params")
    if kwargs["extra_headers"]:
        expected_keys.add("extra_headers")
    expected_keys |= set(kwargs["extra_server_fields"] or {})
    assert set(entry) == expected_keys

    # ... and lands where LiteLLM reads it, with the env-var indirection intact.
    if kwargs["base_url"] is not None:
        assert entry["url"] == kwargs["base_url"]
    else:
        assert entry["url"] == f"os.environ/{kwargs['base_url_env_var']}"
    assert entry["transport"] == kwargs["transport"]
    assert entry["auth_type"] == kwargs["auth_type"]
    if "oauth2_flow" in entry:
        assert entry["oauth2_flow"] == kwargs["oauth2_flow"]
    if "authentication_token" in entry:
        assert (
            entry["authentication_token"] == f"os.environ/{kwargs['authentication_token_env_var']}"
        )
    if "description" in entry:
        assert entry["description"] == kwargs["description"]
    if "extra_headers" in entry:
        assert entry["extra_headers"] == list(kwargs["extra_headers"])
    if "allowed_tools" in entry:
        assert entry["allowed_tools"] == sorted(TOOL_PROFILES[kwargs["profile"]])
    for key, value in (kwargs["extra_server_fields"] or {}).items():
        assert entry[key] == value


@given(
    kwargs=builder_kwargs(),
    bad_flow=st.none() | _TEXT.filter(lambda flow: flow not in _OAUTH2_FLOWS),
)
def test_oauth2_without_a_real_flow_is_rejected_whatever_else_is_passed(
    kwargs: dict[str, Any], bad_flow: str | None
) -> None:
    with pytest.raises(ConfigError, match="oauth2_flow"):
        build_mcp_server_entry(**{**kwargs, "auth_type": "oauth2", "oauth2_flow": bad_flow})


@given(
    kwargs=builder_kwargs(),
    bad_transport=_TEXT.filter(lambda transport: transport not in _TRANSPORTS),
)
def test_non_url_transport_is_rejected_whatever_else_is_passed(
    kwargs: dict[str, Any], bad_transport: str
) -> None:
    # Includes "stdio" and every other string: a caller who bypasses the Literal annotation must
    # never get back a `url`-shaped entry that means nothing to LiteLLM.
    with pytest.raises(ConfigError, match="transport"):
        build_mcp_server_entry(**{**kwargs, "transport": bad_transport})


# --- 3: the force/override_safety scrub is exactly where it must be, and cannot be weakened -----


@given(kwargs=builder_kwargs())
def test_execute_decision_scrub_is_present_exactly_where_the_tool_is_reachable(
    kwargs: dict[str, Any],
) -> None:
    entry = _the_entry(build_mcp_server_entry(**kwargs), kwargs["server_name"])
    allowed = TOOL_PROFILES[kwargs["profile"]]
    reachable = allowed == FULL_PROFILE_SENTINEL or EXECUTE_DECISION in allowed

    assert ("allowed_params" in entry) == reachable
    if reachable:
        scrub = set(entry["allowed_params"][EXECUTE_DECISION])
        assert scrub == EXECUTE_DECISION_MODEL_FACING_PARAMS
        assert not (scrub & NEVER_MODEL_FACING_FIELDS)
        assert set(entry["allowed_params"]) == {EXECUTE_DECISION}
    else:
        assert EXECUTE_DECISION not in entry["allowed_tools"]


@given(kwargs=builder_kwargs(), leaked=st.sampled_from(sorted(NEVER_MODEL_FACING_FIELDS)))
def test_lint_catches_every_weakening_of_the_scrub(kwargs: dict[str, Any], leaked: str) -> None:
    fragment = build_mcp_server_entry(**kwargs)
    entry = _the_entry(fragment, kwargs["server_name"])
    if "allowed_params" not in entry:
        # observe/govern: the tool is unreachable via allowed_tools alone, so there is no scrub to
        # weaken and the linter must stay quiet -- a false positive here would train operators to
        # ignore it.
        assert lint_mcp_server_entry(fragment) == []
        return

    weakened = copy.deepcopy(fragment)
    weakened["mcp_servers"][kwargs["server_name"]]["allowed_params"][EXECUTE_DECISION].append(
        leaked
    )
    violations = lint_mcp_server_entry(weakened)
    assert any("never-model-facing" in v and leaked in v for v in violations), violations

    dropped = copy.deepcopy(fragment)
    del dropped["mcp_servers"][kwargs["server_name"]]["allowed_params"]
    violations = lint_mcp_server_entry(dropped)
    assert any(f"allowed_params.{EXECUTE_DECISION}" in v for v in violations), violations


# --- 4: render_yaml is a faithful serializer with a truthful header -----------------------------


@given(kwargs=builder_kwargs())
def test_render_yaml_round_trips_and_its_header_lists_exactly_the_referenced_env_vars(
    kwargs: dict[str, Any],
) -> None:
    fragment = build_mcp_server_entry(**kwargs)

    assert yaml.safe_load(render_yaml(fragment, with_header=False)) == fragment
    rendered = render_yaml(fragment)
    assert yaml.safe_load(rendered) == fragment  # the header is comments only

    header, sep, _body = rendered.partition("\nmcp_servers:\n")
    assert sep, rendered
    assert all(line.startswith("#") for line in header.splitlines())
    listed = {
        line.removeprefix("#   - ") for line in header.splitlines() if line.startswith("#   - ")
    }
    expected = _referenced_env_vars(fragment)
    assert listed == expected
    assert ("#   (none)" in header) == (not expected)


# --- 5: merge_into_config_file is idempotent and touches only its own entry ---------------------


# Disk I/O per example: a CI runner's filesystem, not this code, decides how long each one takes,
# so Hypothesis's default 200ms deadline is not a meaningful check here.
@settings(deadline=None, max_examples=50)
@given(
    kwargs=builder_kwargs(),
    existing=st.dictionaries(
        _TEXT.filter(lambda key: key != "mcp_servers"), _YAML_VALUE, max_size=3
    ),
    other_servers=st.dictionaries(
        _TEXT, st.dictionaries(_TEXT, _YAML_SCALAR, max_size=3), max_size=2
    ),
)
def test_merge_is_idempotent_and_preserves_everything_it_does_not_own(
    kwargs: dict[str, Any], existing: dict[str, Any], other_servers: dict[str, dict[str, Any]]
) -> None:
    fragment = build_mcp_server_entry(**kwargs)
    server_name = kwargs["server_name"]
    on_disk = {**existing, "mcp_servers": other_servers} if other_servers else dict(existing)

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.yaml"
        config_path.write_text(yaml.safe_dump(on_disk, sort_keys=False), encoding="utf-8")

        first = merge_into_config_file(config_path, fragment)
        second = merge_into_config_file(config_path, fragment)
        assert first == second
        assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == second

    assert second["mcp_servers"][server_name] == fragment["mcp_servers"][server_name]
    assert set(second["mcp_servers"]) == set(other_servers) | {server_name}
    for name, other_entry in other_servers.items():
        if name != server_name:
            assert second["mcp_servers"][name] == other_entry
    for key, value in existing.items():
        assert second[key] == value
    assert set(second) == set(existing) | {"mcp_servers"}


# --- 6: the contract's profile resolver is a monotone filter ------------------------------------

_KNOWN_TOOLS: tuple[str, ...] = tuple(
    sorted(
        set().union(
            *(set(tools) for tools in TOOL_PROFILES.values() if tools != FULL_PROFILE_SENTINEL)
        )
    )
)
# The connected engine may advertise tools the contract has never heard of -- mix those in.
_TOOL_NAME = st.sampled_from(_KNOWN_TOOLS) | st.from_regex(r"\A[a-z][a-z0-9_]{0,20}\Z")


@given(available=st.frozensets(_TOOL_NAME, max_size=12))
def test_resolve_profile_tool_names_is_a_monotone_filter_over_what_the_engine_advertises(
    available: frozenset[str],
) -> None:
    resolved = {
        profile: resolve_profile_tool_names(profile, available_tool_names=available)
        for profile in _PROFILES
    }  # type: ignore[arg-type]

    for profile, names in resolved.items():
        assert names <= available  # never invents a tool
        if profile == "full":
            assert names == available
        else:
            assert names == frozenset(TOOL_PROFILES[profile]) & available  # type: ignore[index]
        assert (EXECUTE_DECISION in names) == (
            EXECUTE_DECISION in available and profile in ("execute", "full")
        )

    assert resolved["observe"] <= resolved["govern"] <= resolved["execute"] <= resolved["full"]
