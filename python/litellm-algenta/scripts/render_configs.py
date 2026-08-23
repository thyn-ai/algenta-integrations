#!/usr/bin/env python3
"""Regenerate `python/litellm-algenta/configs/*.yaml` from `litellm_algenta.config`'s builders.

Run this after a deliberate change to `build_mcp_server_entry`/`render_yaml`'s output, then
re-commit `configs/`. `tests/test_configs_up_to_date.py` fails CI if the checked-in files drift
from what this script would currently produce, so this is the one supported way to update them.

Usage:
    cd python/litellm-algenta
    python3 scripts/render_configs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PACKAGE_ROOT))

from litellm_algenta.config import build_mcp_server_entry, render_yaml  # noqa: E402

_CONFIGS_DIR = _PACKAGE_ROOT / "configs"


def main() -> int:
    _CONFIGS_DIR.mkdir(exist_ok=True)

    for profile in ("observe", "govern", "execute", "full"):
        fragment = build_mcp_server_entry(profile=profile, server_name="algenta")
        path = _CONFIGS_DIR / f"{profile}.yaml"
        path.write_text(render_yaml(fragment), encoding="utf-8")
        print(f"wrote {path.relative_to(_PACKAGE_ROOT)}")

    oauth2_fragment = build_mcp_server_entry(
        profile="observe",
        server_name="algenta",
        auth_type="oauth2",
        oauth2_flow="client_credentials",
        authentication_token_env_var=None,
        extra_server_fields={
            "token_url": "os.environ/ALGENTA_OAUTH2_TOKEN_URL",
            "client_id": "os.environ/ALGENTA_OAUTH2_CLIENT_ID",
            "client_secret": "os.environ/ALGENTA_OAUTH2_CLIENT_SECRET",
        },
    )
    oauth2_path = _CONFIGS_DIR / "observe.oauth2-client-credentials.yaml"
    oauth2_path.write_text(render_yaml(oauth2_fragment), encoding="utf-8")
    print(f"wrote {oauth2_path.relative_to(_PACKAGE_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
