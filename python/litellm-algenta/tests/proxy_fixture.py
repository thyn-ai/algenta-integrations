"""Spawn a real `litellm --config ...` proxy process for the conformance test suite.

This is the one thing that makes this package's tests structurally heavier than its
`pydantic-ai-algenta` / `langchain-algenta` siblings: those packages exercise their own Python
wrapper code in-process against a real stub MCP server. This package has no wrapper code to run
in-process -- the actual "product" under test is a config fragment plus a real, external `litellm`
gateway *process* -- so its conformance suite has to start that real process too, not just the
stub server. See the package README's "Testing this package" section for the CI-time-budget
tradeoff this implies.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

MASTER_KEY = "sk-algenta-litellm-conformance-test"


def _find_litellm_executable() -> str:
    """Locate the `litellm` console script installed alongside the running interpreter.

    Prefers the script next to `sys.executable` (i.e. this package's own `.venv/bin/litellm`,
    installed by `litellm[proxy]` being a real dev dependency) over a bare `PATH` lookup, since a
    test run under `uv run pytest` / `.venv/bin/pytest` should use *that* venv's litellm, not
    whatever else might be first on `PATH`.
    """
    candidate = Path(sys.executable).with_name("litellm")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("litellm")
    if found:
        return found
    raise RuntimeError(
        "no 'litellm' executable found next to sys.executable or on PATH -- is litellm[proxy] "
        "installed in this environment (dev extras via `uv sync --all-packages --all-extras`)?"
    )


def _free_port() -> int:
    """Allocate a free TCP port by binding then immediately releasing it.

    Unlike the stub server (which hands its already-bound socket directly to fastmcp, avoiding
    any race), the litellm proxy is a subprocess we don't control the binding of -- so this has
    an inherent, small bind-then-release-then-reuse race window. Standard practice for spawning
    an external server in a test; a collision is rare enough not to warrant a retry loop here.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiteLLMProxyFixture:
    """Async context manager: writes `config`, starts a real `litellm` proxy against it, waits
    for it to actually be serving MCP requests, and tears it down afterward.

    `config` is a full litellm config dict (typically built by merging one or more
    `litellm_algenta.config.build_mcp_server_entry` fragments together under one `mcp_servers:`
    key) -- this fixture only handles the process lifecycle, not the config's content.
    """

    def __init__(self, config: dict[str, Any], *, config_dir: Path, env: dict[str, str] | None = None) -> None:
        self._config = config
        self._config_dir = config_dir
        self._extra_env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_lines: list[str] = []
        self._reader_task: asyncio.Task[None] | None = None
        self.port: int = 0
        self.base_url: str = ""
        self.master_key: str = MASTER_KEY

    @property
    def captured_output(self) -> str:
        return "".join(self._stdout_lines)

    async def __aenter__(self) -> LiteLLMProxyFixture:
        config = dict(self._config)
        config.setdefault("general_settings", {})
        config["general_settings"] = {**config["general_settings"], "master_key": self.master_key}

        config_path = self._config_dir / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, default_flow_style=False), encoding="utf-8")

        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        litellm_exe = _find_litellm_executable()
        env = {**os.environ, **self._extra_env}

        self._proc = await asyncio.create_subprocess_exec(
            litellm_exe,
            "--config",
            str(config_path),
            "--port",
            str(self.port),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._reader_task = asyncio.create_task(self._drain_output())

        await self._wait_until_ready()
        return self

    async def _drain_output(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            self._stdout_lines.append(line.decode(errors="replace"))

    async def _wait_until_ready(self, *, timeout: float = 45.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        last_error: Exception | None = None
        async with httpx.AsyncClient() as client:
            while asyncio.get_event_loop().time() < deadline:
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"litellm proxy exited early (code {self._proc.returncode}) before "
                        f"becoming ready:\n{self.captured_output}"
                    )
                try:
                    resp = await client.get(
                        f"{self.base_url}/mcp-rest/tools/list",
                        headers={"Authorization": f"Bearer {self.master_key}"},
                        timeout=2.0,
                    )
                    if resp.status_code == 200:
                        return
                    last_error = RuntimeError(f"tools/list returned {resp.status_code}: {resp.text}")
                except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout) as exc:
                    last_error = exc
                await asyncio.sleep(0.25)
        raise TimeoutError(
            f"litellm proxy on {self.base_url} never became ready within {timeout}s "
            f"(last error: {last_error}); captured output:\n{self.captured_output}"
        )

    async def __aexit__(self, *exc_info: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
