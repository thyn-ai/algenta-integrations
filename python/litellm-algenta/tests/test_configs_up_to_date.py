"""Guards against the checked-in `configs/*.yaml` templates drifting from what
`build_mcp_server_entry`/`render_yaml` currently produce.

Per this repo's own "dynamic count tests" convention: never hand-maintain a value (here, a whole
generated file) that has a real source of truth elsewhere without a test tying the two together.
Regenerating `configs/` is `scripts/render_configs.py`'s job (run it and re-commit if one of these
fails after a deliberate change to `config.py`'s output) -- this test only detects drift, it does
not fix it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from litellm_algenta.config import build_mcp_server_entry, render_yaml

_CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"


@pytest.mark.parametrize("profile", ["observe", "govern", "execute", "full"])
def test_profile_template_matches_current_builder_output(profile: str) -> None:
    path = _CONFIGS_DIR / f"{profile}.yaml"
    assert path.is_file(), f"missing configs/{profile}.yaml -- run scripts/render_configs.py"
    expected = render_yaml(build_mcp_server_entry(profile=profile, server_name="algenta"))
    assert path.read_text(encoding="utf-8") == expected, (
        f"configs/{profile}.yaml is stale relative to build_mcp_server_entry()'s current output "
        f"-- run scripts/render_configs.py and commit the result"
    )


def test_oauth2_example_template_matches_current_builder_output() -> None:
    path = _CONFIGS_DIR / "observe.oauth2-client-credentials.yaml"
    assert path.is_file()
    expected = render_yaml(
        build_mcp_server_entry(
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
    )
    assert path.read_text(encoding="utf-8") == expected
