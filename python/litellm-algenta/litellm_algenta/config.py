"""Build, lint, and merge LiteLLM MCP Gateway `mcp_servers:` config entries for Algenta.

LiteLLM's MCP Gateway (`litellm --config config.yaml`, its real, current `mcp_servers:` feature --
see `litellm/proxy/_experimental/mcp_server/mcp_server_manager.py::load_servers_from_config`,
called from `proxy_server.py` at startup) is a proxy/gateway *server process configured by YAML*,
not a library this package's code calls in-process the way `pydantic-ai-algenta` wraps
`pydantic_ai.mcp.MCPToolset` or `langchain-algenta` wraps `MultiServerMCPClient`. There is no
`AlgentaToolset` object to build here. What this module ships instead is the honest equivalent for
a gateway integration: a generator for the one real config surface that maps the shared tool-profile
contract onto the gateway's own enforcement, plus a linter that catches an unsafe config before a
`litellm` process ever starts.

Everything below reflects **verified, current (litellm 1.98.0) gateway behavior**, confirmed two
ways: by reading the installed package's source (file/line cited inline) and, for the load-bearing
claims, by running a real `litellm --config ...` proxy process against a real stub MCP server and
inspecting the real HTTP responses (see `tests/test_gateway_conformance.py`, which repeats these
exact checks as an executable test suite rather than asking you to trust this docstring).

## What LiteLLM's gateway config can genuinely enforce (and what this module fills in for it)

- **`allowed_tools`** -- a real, per-server, call-time allowlist. Confirmed as more than a listing
  filter: calling a tool not on the list returns a genuine `403` from
  `MCPServerManager.check_allowed_or_banned_tools` (`mcp_server_manager.py`), not just an absence
  from `tools/list`. `build_mcp_server_entry` maps this repo's four contract profiles
  (`contracts/integration-tool-contract.json`) directly onto this field -- see
  `litellm_algenta.contract.TOOL_PROFILES`. For `"full"`, `allowed_tools` is *omitted* entirely
  (LiteLLM's own meaning of "no restriction configured"), matching the contract's `tools: "*"`.

- **`allowed_params.<tool_name>`** -- a real, per-tool, call-time argument allowlist. Confirmed by
  reading `MCPServerManager.validate_allowed_params` (`mcp_server_manager.py`, called from
  `call_tool` before the upstream request is ever made) and by a live proxy run: an
  `execute_decision` call carrying a `force` argument against a server configured with
  `allowed_params: {execute_decision: ["decision_id", "webhook_url"]}` gets a real `403`
  (`"Parameters ['force'] are not allowed for tool execute_decision. ..."`) -- the argument never
  reaches the upstream Algenta MCP server at all. **`build_mcp_server_entry` sets this
  automatically, on every profile under which `execute_decision` is reachable (`execute` and
  `full` -- `govern` and `observe` never expose the tool at all), to exactly
  `contract.EXECUTE_DECISION_MODEL_FACING_PARAMS`.** This is deliberately unconditional on
  profile, including `full`: the contract's `never_model_facing_note` says `force`/
  `override_safety` may never be model-reachable "in ANY profile, ever" -- not just the ones this
  package considers default-safe -- so `full`'s opt-in breadth does not get a pass on this one
  rule. `lint_mcp_server_entry` re-checks this on any config you hand it (including one you wrote
  by hand, not through `build_mcp_server_entry`) and reports a violation if it's missing or
  incomplete.

## What LiteLLM's gateway config genuinely CANNOT enforce -- said plainly, not glossed over

- **It cannot strip `force`/`override_safety` from the *advertised* JSON Schema.** `allowed_params`
  gates *arguments actually sent*, not what `GET /mcp-rest/tools/list` (or the native `/mcp`
  aggregate surface) *shows* the model as available on `execute_decision`'s schema -- confirmed by
  reading the discovery-handling code path in `litellm/proxy/_experimental/mcp_server/server.py`,
  which never consults `allowed_params` at all. If the connected Algenta engine's own MCP tool
  registry advertises `force` on `execute_decision`'s schema (the contract says it may, for
  operator/break-glass use), a model talking through this gateway will still *see* `force` listed
  as a parameter it could try to set -- it just cannot make that argument actually take effect,
  because `allowed_params` will 403 the call before it reaches the engine. Getting the schema
  itself clean is the connected engine's job (serve a model-facing schema that never advertises
  these fields to begin with, or run this package's config in front of a schema-stripping proxy of
  your own) -- not something any `mcp_servers:` config key can do, and this package does not
  pretend otherwise.
- **There is no approval-pause primitive, and the real tool never needs one.** `execute_decision`
  is synchronous, full stop: a call either comes back `200` with a real `ExecutionReceipt`
  (`decision_id`/`webhook_url`/`execution_status`/`response_code`/`executed_at`/
  `policy_snapshot_id`/`schema_snapshot_id`/`manifest_version`/`payload_summary`/
  `safety_overridden`) or is refused outright, in that same call, as a `409` naming exactly one of
  three real gates (`"idempotency"`, `"confidence"`, `"risk_floor"` -- see
  `litellm_algenta.contract.EXECUTE_DECISION_GATES`) -- never a `pending`/`rejected`/`expired`
  state to come back and check on later. A JSON-RPC `tools/call` result has no HTTP status of its
  own, so the only way that `409` denial can appear on this transport is the same way any other
  tool-body exception does: a real MCP-fronting `execute_decision` implementation surfaces it as
  `isError: true`, with the gate/code/message/override_hint inside the result content -- and this
  gateway passes that through completely unchanged, exactly like the already-verified
  static-credential-401 case below (see
  `tests/test_gateway_conformance.py::test_execute_decision_gate_denial_surfaces_as_iserror`). A
  successful `200` receipt passes through just as unchanged, `isError: false`. There is no
  LiteLLM-native equivalent of `pydantic_ai_algenta`'s `ApprovalRequired` or `langchain_algenta`'s
  `interrupt()`-based pause, and this tool never has anything for one of those to pause on. Whatever
  built the chat-completion request that triggered the tool call is entirely on its own to notice
  `isError` and read the result -- there is no config key, callback, or webhook this package could
  wire up that would change that, because the gateway is receipt-blind by design (it has no opinion
  on what's inside a tool's JSON result).

## Env-var-first, self-hosted-only by construction

`build_mcp_server_entry` never accepts or emits a literal secret value or an Algenta-hosted URL.
`url` and (for the static-credential auth types) `authentication_token` are always emitted as
LiteLLM's own `os.environ/VAR_NAME` placeholder convention -- confirmed live: a config carrying
`authentication_token: "os.environ/ALGENTA_MCP_TOKEN"` resolves to the real environment variable's
value by the time the upstream request is sent (`proxy_server.py`'s `_check_for_os_environ_vars`,
which walks the whole loaded config recursively, `mcp_servers:` included). `lint_mcp_server_entry`
also flags a `url` that looks like an Algenta-hosted endpoint (`algenta.ai`) even if you bypassed
this builder and wrote the entry by hand.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

from .contract import (
    EXECUTE_DECISION,
    EXECUTE_DECISION_MODEL_FACING_PARAMS,
    FULL_PROFILE_SENTINEL,
    NEVER_MODEL_FACING_FIELDS,
    TOOL_PROFILES,
    DEFAULT_PROFILE,
    ToolProfile,
)

__all__ = [
    "AuthType",
    "ConfigError",
    "DEFAULT_BASE_URL_ENV_VAR",
    "DEFAULT_SERVER_NAME",
    "DEFAULT_TOKEN_ENV_VAR",
    "build_mcp_server_entry",
    "lint_mcp_server_entry",
    "merge_into_config_file",
    "render_yaml",
]

DEFAULT_SERVER_NAME = "algenta"
#: Never a literal URL -- the env var LiteLLM resolves at proxy-startup time (see module
#: docstring's "Env-var-first" section). Point it at your own self-hosted engine's `/mcp` endpoint.
DEFAULT_BASE_URL_ENV_VAR = "ALGENTA_MCP_URL"
DEFAULT_TOKEN_ENV_VAR = "ALGENTA_MCP_TOKEN"
DEFAULT_TRANSPORT: Literal["http"] = "http"
DEFAULT_EXTRA_HEADERS: tuple[str, ...] = ("x-trace-id",)

#: `auth_type` values LiteLLM's `mcp_server_manager.py` recognizes (`litellm/types/mcp.py`).
#: `bearer_token`/`api_key`/`basic`/`token` are static-credential modes (a mid-session upstream
#: 401 becomes a swallowed `isError: true` tool result, no retry -- see this package's README);
#: `oauth2` (with `oauth2_flow`), `oauth2_token_exchange`, and `oauth2_id_jag` mint/refresh tokens
#: server-side; `true_passthrough`/`oauth_delegate` forward the caller's own upstream OAuth token
#: unchanged and are the only modes where a 401 is relayed to the caller as a clean, retryable
#: `401` instead of being swallowed.
AuthType = Literal[
    "bearer_token",
    "api_key",
    "basic",
    "token",
    "oauth2",
    "true_passthrough",
    "oauth_delegate",
    "oauth2_token_exchange",
    "oauth2_id_jag",
    "aws_sigv4",
]

_STATIC_CREDENTIAL_AUTH_TYPES: frozenset[str] = frozenset({"bearer_token", "api_key", "basic", "token"})
_OAUTH2_FLOWS: frozenset[str] = frozenset({"client_credentials", "authorization_code"})
_ALGENTA_HOSTED_URL_MARKERS: tuple[str, ...] = ("algenta.ai",)


class ConfigError(ValueError):
    """Raised by `build_mcp_server_entry` (invalid input) or `assert_safe` (an unsafe config)."""


def _profile_allowed_tools(profile: ToolProfile) -> list[str] | None:
    """The `allowed_tools` value for `profile`, or `None` to mean "omit the key entirely"."""
    if profile not in TOOL_PROFILES:
        raise ConfigError(f"unknown profile {profile!r}; must be one of {sorted(TOOL_PROFILES)}")
    allowed = TOOL_PROFILES[profile]
    if allowed == FULL_PROFILE_SENTINEL:
        return None
    return sorted(allowed)


def build_mcp_server_entry(
    *,
    profile: ToolProfile = DEFAULT_PROFILE,
    server_name: str = DEFAULT_SERVER_NAME,
    base_url_env_var: str = DEFAULT_BASE_URL_ENV_VAR,
    base_url: str | None = None,
    transport: Literal["http", "sse"] = DEFAULT_TRANSPORT,
    description: str | None = None,
    auth_type: AuthType = "bearer_token",
    authentication_token_env_var: str | None = DEFAULT_TOKEN_ENV_VAR,
    oauth2_flow: Literal["client_credentials", "authorization_code"] | None = None,
    extra_headers: Iterable[str] = DEFAULT_EXTRA_HEADERS,
    extra_server_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one `{"mcp_servers": {server_name: {...}}}` fragment for a self-hosted Algenta engine.

    Args:
        profile: One of the contract's four profiles. Resolved to LiteLLM's `allowed_tools` (see
            module docstring). Defaults to `"observe"`, matching every other package in this repo.
        server_name: The key under `mcp_servers:` this entry is registered as.
        base_url_env_var: Env var LiteLLM resolves at startup for the upstream `url` -- never a
            literal endpoint (see "Env-var-first" in the module docstring). Ignored if `base_url`
            is given explicitly (only ever do that for a config you don't intend to check in, e.g.
            a test fixture pointed at a local stub).
        base_url: An explicit literal URL, escaping the env-var indirection. Rejected by
            `lint_mcp_server_entry` if it looks like an Algenta-hosted address.
        transport: `"http"` (streamable-http, the default and the only transport this package's
            own tests exercise) or `"sse"` -- both are URL-addressed, so both fit this function's
            `url`-based shape. **Deliberately excludes `"stdio"`**: LiteLLM's real `stdio`
            transport is configured via a `command`/`args`/`env` shape
            (`litellm.types.mcp.MCPStdioConfig`, confirmed by reading
            `mcp_server_manager.py`'s `stdio_config` construction), not a `url` at all -- there is
            no sane way for this function to honor a `transport="stdio"` request through the
            `url`/`base_url_env_var` parameters it actually has, and silently emitting a `url` key
            on a `stdio` entry would produce a config that means nothing to LiteLLM. Pass
            `extra_server_fields={"command": ..., "args": [...], "env": {...}}` together with a
            hand-built fragment instead if you need `stdio` (out of scope for this package's own
            self-hosted-over-HTTP positioning, but not blocked).
        auth_type: One of LiteLLM's real `auth_type` values (`AuthType`). Only the
            static-credential modes (`bearer_token`/`api_key`/`basic`/`token`) use
            `authentication_token_env_var`; `oauth2` requires `oauth2_flow` (LiteLLM itself
            rejects an `oauth2` entry with no flow at proxy-startup time -- this function raises
            the same requirement earlier, at build time). The other auth types
            (`true_passthrough`, `oauth_delegate`, `oauth2_token_exchange`, `oauth2_id_jag`,
            `aws_sigv4`) need fields this function doesn't model directly -- pass them via
            `extra_server_fields`.
        authentication_token_env_var: Env var holding the raw bearer/api-key/basic/token
            credential (no scheme prefix -- LiteLLM adds `"Bearer "`/`"ApiKey "`/etc. itself, per
            `mcp_server_manager.py`). Pass `None` to omit `authentication_token` entirely (e.g.
            for an upstream that needs no credential, or when supplying one via
            `extra_server_fields` instead).
        extra_headers: Caller-set HTTP headers LiteLLM forwards from the model-calling client to
            the upstream MCP server (confirmed real and verbatim -- see the module docstring's
            trace-header note; response headers do NOT round-trip the other way, so any upstream
            signal you need back has to travel inside the JSON receipt body instead -- correlate
            by the receipt's own `decision_id` echo-back). Defaults to `("x-trace-id",)`.
        extra_server_fields: Additional literal `mcp_servers.<server_name>` fields this function
            doesn't model directly (e.g. `token_url`/`client_id`/`client_secret` for `oauth2`,
            `dcr_bridge` for `true_passthrough`/`oauth_delegate`). Raises `ConfigError` if a key
            here collides with one this function already sets, rather than silently overriding it.

    Returns:
        A dict shaped `{"mcp_servers": {server_name: {...}}}`, ready for `render_yaml` or
        `merge_into_config_file`.

    Raises:
        ConfigError: `profile` is not a real profile name; `transport` is `"stdio"` (or any other
            value this function's `url`-based shape can't express -- see the `transport` arg
            above); `auth_type == "oauth2"` with no (or an invalid) `oauth2_flow`; or
            `extra_server_fields` collides with a field already set.
    """
    if transport not in ("http", "sse"):
        raise ConfigError(
            f"transport={transport!r} is not supported by this function's url-based shape "
            "(only 'http' and 'sse' are -- both are URL-addressed). 'stdio' in particular needs "
            "a command/args/env shape (litellm.types.mcp.MCPStdioConfig), which this function "
            "does not build; construct that fragment by hand with extra_server_fields instead of "
            "asking this function to emit a meaningless url for it. This check runs even though "
            "the type annotation already narrows the accepted values, since Python does not "
            "enforce Literal types at runtime and a caller who bypasses type checking must not "
            "silently get back a config that means nothing to LiteLLM."
        )

    entry: dict[str, Any] = {
        "url": base_url if base_url is not None else f"os.environ/{base_url_env_var}",
        "transport": transport,
    }
    if description:
        entry["description"] = description

    entry["auth_type"] = auth_type
    if auth_type == "oauth2":
        if oauth2_flow not in _OAUTH2_FLOWS:
            raise ConfigError(
                "auth_type='oauth2' requires oauth2_flow to be 'client_credentials' "
                "(machine-to-machine) or 'authorization_code' (interactive, per-user) -- "
                "verified directly against a real litellm 1.98.0 proxy: starting one with "
                "auth_type='oauth2' and no oauth2_flow crashes at startup with exactly this "
                "message from mcp_server_manager.py. This raises the same requirement here, "
                "at config-build time, so a bad config fails your build/test step instead of "
                "a running proxy."
            )
        entry["oauth2_flow"] = oauth2_flow
    elif auth_type in _STATIC_CREDENTIAL_AUTH_TYPES and authentication_token_env_var:
        entry["authentication_token"] = f"os.environ/{authentication_token_env_var}"

    allowed_tools = _profile_allowed_tools(profile)
    execute_decision_reachable = allowed_tools is None or EXECUTE_DECISION in allowed_tools
    if allowed_tools is not None:
        entry["allowed_tools"] = allowed_tools

    if execute_decision_reachable:
        # Unconditional on profile -- see module docstring on why "full" gets this too.
        entry["allowed_params"] = {EXECUTE_DECISION: sorted(EXECUTE_DECISION_MODEL_FACING_PARAMS)}

    if extra_headers:
        entry["extra_headers"] = list(extra_headers)

    if extra_server_fields:
        collisions = sorted(set(extra_server_fields) & set(entry))
        if collisions:
            raise ConfigError(
                f"extra_server_fields collides with field(s) this function already set: "
                f"{collisions} -- pass a different value through this function's own "
                f"parameter instead of overriding it via extra_server_fields."
            )
        entry.update(extra_server_fields)

    return {"mcp_servers": {server_name: entry}}


_YAML_HEADER_TEMPLATE = """\
# Generated by litellm_algenta.config.build_mcp_server_entry -- see
# python/litellm-algenta/README.md for the full picture (auth types, profile enforcement,
# receipt passthrough, trace headers, token-expiry behavior).
#
# Self-hosted only: nothing below points at an Algenta-hosted endpoint. Set these in the
# environment the `litellm` proxy process runs in before starting it:
{env_var_lines}"""


def render_yaml(fragment: dict[str, Any], *, with_header: bool = True) -> str:
    """Render a `build_mcp_server_entry`-shaped fragment to a YAML document.

    With `with_header=True` (the default), prepends a comment block naming every
    `os.environ/VAR_NAME` placeholder this fragment actually references, so a reader knows
    exactly what to set before running `litellm --config` against it -- generated from the
    fragment's own content, never a separately-maintained list that could drift out of sync
    with it (see this repo's "dynamic count tests" convention).
    """
    body = yaml.safe_dump(fragment, sort_keys=False, default_flow_style=False)
    if not with_header:
        return body
    env_vars = sorted(_referenced_env_vars(fragment))
    env_var_lines = "\n".join(f"#   - {name}" for name in env_vars) if env_vars else "#   (none)"
    header = _YAML_HEADER_TEMPLATE.format(env_var_lines=env_var_lines)
    return f"{header}\n\n{body}"


def _referenced_env_vars(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str) and value.startswith("os.environ/"):
        found.add(value[len("os.environ/") :])
    elif isinstance(value, dict):
        for v in value.values():
            found |= _referenced_env_vars(v)
    elif isinstance(value, list):
        for v in value:
            found |= _referenced_env_vars(v)
    return found


def _looks_like_algenta_hosted_url(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in _ALGENTA_HOSTED_URL_MARKERS)


def lint_mcp_server_entry(fragment: dict[str, Any]) -> list[str]:
    """Return a list of human-readable violations in an `mcp_servers:` fragment; empty if clean.

    Runs against *any* `{"mcp_servers": {...}}`-shaped dict, not only ones `build_mcp_server_entry`
    produced -- the point is to catch a hand-edited or hand-written config too, e.g. one where an
    operator added `allowed_tools` back in without noticing it now needs a matching
    `allowed_params.execute_decision`. Checks, per server entry:

    1. If `execute_decision` is reachable (`allowed_tools` omits it entirely -- the "full"
       profile's shape -- or explicitly includes it), `allowed_params.execute_decision` must be
       set and must not include any `NEVER_MODEL_FACING_FIELDS` member.
    2. `auth_type == "oauth2"` must carry a valid `oauth2_flow`.
    3. `url` must not look like an Algenta-hosted endpoint (`algenta.ai`), whether or not it's
       wrapped in an `os.environ/` placeholder.
    """
    violations: list[str] = []
    servers = fragment.get("mcp_servers") or {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            violations.append(f"mcp_servers.{name}: expected a mapping, got {type(entry).__name__}")
            continue

        allowed_tools = entry.get("allowed_tools")
        execute_decision_reachable = allowed_tools is None or EXECUTE_DECISION in allowed_tools
        if execute_decision_reachable:
            allowed_params = entry.get("allowed_params") or {}
            execute_params = allowed_params.get(EXECUTE_DECISION)
            if execute_params is None:
                violations.append(
                    f"mcp_servers.{name}: execute_decision is reachable (allowed_tools "
                    f"omits or includes it) but allowed_params.{EXECUTE_DECISION} is not set -- "
                    f"a model-supplied 'force' or 'override_safety' argument would reach the "
                    f"upstream engine unfiltered. Set allowed_params.{EXECUTE_DECISION} to "
                    f"{sorted(EXECUTE_DECISION_MODEL_FACING_PARAMS)}."
                )
            else:
                leaked = sorted(set(execute_params) & NEVER_MODEL_FACING_FIELDS)
                if leaked:
                    violations.append(
                        f"mcp_servers.{name}: allowed_params.{EXECUTE_DECISION} includes "
                        f"never-model-facing field(s) {leaked} -- these must never be reachable "
                        f"via a model-supplied argument, in any profile, ever."
                    )

        auth_type = entry.get("auth_type")
        if auth_type == "oauth2" and entry.get("oauth2_flow") not in _OAUTH2_FLOWS:
            violations.append(
                f"mcp_servers.{name}: auth_type='oauth2' needs oauth2_flow set to "
                f"'client_credentials' or 'authorization_code'."
            )

        url = entry.get("url")
        if isinstance(url, str) and _looks_like_algenta_hosted_url(url):
            violations.append(
                f"mcp_servers.{name}: url {url!r} looks like an Algenta-hosted endpoint, not a "
                f"self-hosted one -- every package in this repository must default to the "
                f"caller's own deployment."
            )

        if entry.get("transport") == "stdio" and "url" in entry:
            violations.append(
                f"mcp_servers.{name}: transport='stdio' but a 'url' key is set -- LiteLLM's real "
                f"stdio transport is configured via command/args/env (litellm.types.mcp."
                f"MCPStdioConfig), not a url; a url alongside transport='stdio' means nothing to "
                f"LiteLLM and this entry will not do what its 'url' value implies."
            )

    return violations


def assert_safe(fragment: dict[str, Any]) -> None:
    """Raise `ConfigError` if `lint_mcp_server_entry(fragment)` reports any violation."""
    violations = lint_mcp_server_entry(fragment)
    if violations:
        rendered = "\n".join(f"  - {v}" for v in violations)
        raise ConfigError(f"unsafe mcp_servers config:\n{rendered}")


def merge_into_config_file(config_path: Path, fragment: dict[str, Any]) -> dict[str, Any]:
    """Idempotently merge `fragment["mcp_servers"]` into `config_path` (created if absent).

    Only the `mcp_servers` key is touched -- `model_list`, `general_settings`, and everything
    else already in the file is preserved untouched. Re-running with the same `server_name` is
    idempotent: it overwrites that one entry in place rather than duplicating it.

    Returns the full merged config dict actually written.
    """
    if config_path.exists():
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(existing, dict):
            raise ConfigError(f"{config_path}: expected a YAML mapping at the top level")
    else:
        existing = {}

    existing.setdefault("mcp_servers", {})
    if not isinstance(existing["mcp_servers"], dict):
        raise ConfigError(f"{config_path}: existing 'mcp_servers' key is not a mapping")
    existing["mcp_servers"].update(fragment.get("mcp_servers", {}))

    config_path.write_text(
        yaml.safe_dump(existing, sort_keys=False, default_flow_style=False), encoding="utf-8"
    )
    return existing


def main(argv: list[str] | None = None) -> int:
    """`python -m litellm_algenta.config` -- generate or merge one server entry from the CLI."""
    parser = argparse.ArgumentParser(
        prog="python -m litellm_algenta.config",
        description="Generate (or idempotently merge) a LiteLLM mcp_servers: entry for a "
        "self-hosted Algenta Engine.",
    )
    parser.add_argument("--profile", choices=sorted(TOOL_PROFILES), default=DEFAULT_PROFILE)
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    parser.add_argument("--base-url-env-var", default=DEFAULT_BASE_URL_ENV_VAR)
    parser.add_argument("--auth-type", default="bearer_token")
    parser.add_argument("--token-env-var", default=DEFAULT_TOKEN_ENV_VAR)
    parser.add_argument(
        "--merge-into",
        type=Path,
        default=None,
        help="An existing (or not-yet-created) litellm config.yaml to merge this entry into "
        "in place, instead of printing standalone YAML to stdout.",
    )
    parser.add_argument(
        "--no-lint",
        action="store_true",
        help="Skip the assert_safe() check before emitting/merging (not recommended).",
    )
    args = parser.parse_args(argv)

    fragment = build_mcp_server_entry(
        profile=args.profile,
        server_name=args.server_name,
        base_url_env_var=args.base_url_env_var,
        auth_type=args.auth_type,
        authentication_token_env_var=args.token_env_var,
    )
    if not args.no_lint:
        assert_safe(fragment)

    if args.merge_into is None:
        sys.stdout.write(render_yaml(fragment))
        return 0

    merge_into_config_file(args.merge_into, fragment)
    print(f"merged mcp_servers.{args.server_name} ({args.profile} profile) into {args.merge_into}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
