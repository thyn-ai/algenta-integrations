"""litellm-algenta: LiteLLM MCP Gateway config generation for a self-hosted Algenta Engine.

There is no in-process toolset to import here -- LiteLLM's MCP Gateway is a proxy/gateway
*server process* configured by YAML, not a library your application imports and calls (see
`litellm_algenta.config`'s module docstring for the full reasoning and the verified-real gateway
behavior this package's config choices are based on). What this package provides:

- `build_mcp_server_entry` / `render_yaml` / `merge_into_config_file` -- generate (or merge into
  an existing config) a real `mcp_servers:` entry pointed at your own self-hosted Algenta engine,
  with the shared tool-profile contract mapped onto LiteLLM's real `allowed_tools` /
  `allowed_params` enforcement.
- `lint_mcp_server_entry` / `assert_safe` -- catch an unsafe config (missing the
  `force`/`override_safety` argument scrub, an Algenta-hosted URL, a malformed `oauth2` block)
  before a `litellm` process ever starts.
- `contract` -- this repo's shared tool-profile contract, embedded for runtime use (same pattern
  as the `pydantic_ai_algenta.contract` / `langchain_algenta.contract` siblings).

See `configs/` for ready-to-use, per-profile config templates, and this package's README for the
honest accounting of what LiteLLM's gateway does and doesn't enforce natively.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from . import contract
from .config import (
    DEFAULT_BASE_URL_ENV_VAR,
    DEFAULT_SERVER_NAME,
    DEFAULT_TOKEN_ENV_VAR,
    AuthType,
    ConfigError,
    assert_safe,
    build_mcp_server_entry,
    lint_mcp_server_entry,
    merge_into_config_file,
    render_yaml,
)

try:
    # Read from the installed distribution's metadata (populated from pyproject.toml's
    # [project].version at build time) instead of a second hardcoded literal here -- the two
    # already drifted apart in sibling packages in this repo (a hardcoded __version__ left
    # unchanged while pyproject.toml moved on), which is exactly the class of bug this avoids.
    __version__ = _pkg_version("litellm-algenta")
except PackageNotFoundError:
    # Not installed (e.g. running straight from a source checkout with no editable install) --
    # fall back to a clearly-marked placeholder rather than a guessed version number.
    __version__ = "0.0.0+unknown"

__all__ = [
    "AuthType",
    "ConfigError",
    "DEFAULT_BASE_URL_ENV_VAR",
    "DEFAULT_SERVER_NAME",
    "DEFAULT_TOKEN_ENV_VAR",
    "__version__",
    "assert_safe",
    "build_mcp_server_entry",
    "contract",
    "lint_mcp_server_entry",
    "merge_into_config_file",
    "render_yaml",
]
