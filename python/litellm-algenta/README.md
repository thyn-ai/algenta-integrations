# litellm-algenta

[![PyPI](https://img.shields.io/pypi/v/litellm-algenta.svg)](https://pypi.org/project/litellm-algenta/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](../../LICENSE)

> **Docs:** [docs.algenta.ai](https://docs.algenta.ai) · [All integrations](../../README.md)

LiteLLM MCP Gateway integration for [Algenta](https://algenta.ai): config templates, a config
generator/linter, and a real-proxy conformance test suite that map
[`contracts/integration-tool-contract.json`](../../contracts/integration-tool-contract.json)'s
tool-profile boundary onto LiteLLM's real `mcp_servers:` gateway config, pointed at your own
self-hosted Algenta Engine.

**This package is not shaped like its `pydantic-ai-algenta` / `langchain-algenta` /
`algenta-tools` (Vercel AI SDK) siblings, on purpose.** LiteLLM's MCP Gateway
(`litellm --config config.yaml`) is a proxy/gateway *server process configured by YAML* -- there
is no `AlgentaToolset` object, no `create_algenta_tools` function, no in-process wrapper to call.
What this package ships instead:

- **`litellm_algenta.config.build_mcp_server_entry`** -- generates a real `mcp_servers:` entry
  for your own self-hosted Algenta engine, with `contracts/integration-tool-contract.json`'s four
  profiles mapped onto LiteLLM's real, call-time-enforced `allowed_tools` (and, for
  `execute_decision`, `allowed_params`).
- **`litellm_algenta.config.lint_mcp_server_entry` / `assert_safe`** -- catch an unsafe config
  (missing the `force`/`override_safety` argument scrub, an Algenta-hosted URL, a malformed
  `oauth2` block) before a `litellm` process ever starts, whether or not you built it with this
  package's own generator.
- **[`configs/`](./configs)** -- ready-to-use YAML templates, one per profile, plus an OAuth2
  (`client_credentials`) example -- see [Config templates](#config-templates).
- **A real-proxy conformance test suite** (`tests/`) that starts a real `litellm` proxy process
  against a real stub Algenta MCP server and asserts against the gateway's actual HTTP behavior --
  see [Testing this package](#testing-this-package).

## Prerequisites

- **A self-hosted Algenta Engine, already running and reachable.** This package only generates
  and validates a config fragment that points at it -- it never runs an engine for you, and it
  never talks to any Algenta-operated service. There is no Algenta-hosted API and no Algenta
  account to sign up for.
- **That engine's MCP endpoint URL and bearer credential** (or OAuth2 client credentials, if
  your engine uses those instead) -- set as environment variables, never as literal values in a
  config file (see [Self-hosted-first](#self-hosted-first) below).
- **Python 3.10+.**
- **`litellm[proxy]`, installed separately, only once you're ready to actually run the gateway**
  (see [Install](#install) below) -- generating and linting a config needs nothing but this
  package itself.

## Install

```bash
pip install litellm-algenta
```

This package's own runtime code depends on nothing but `pyyaml` -- not `litellm` itself, and not
`algenta-sdk` (see [Why no `algenta-sdk` dependency](#why-no-algenta-sdk-dependency) below).
`build_mcp_server_entry`/`render_yaml` work standalone, e.g. to generate a config fragment for a
`litellm` process that runs somewhere else entirely (a different host, a managed LiteLLM
deployment). Running the gateway yourself needs `litellm[proxy]` separately:

```bash
pip install 'litellm[proxy]'
```

This one pulls in LiteLLM's full proxy-server dependency tree (the proxy server itself, database
clients, and other packages the gateway process needs) -- several hundred MB and a minute or more
to install, not a mistake or a hang.

## Self-hosted-first

Every config this package generates points at **your own self-hosted Algenta Engine**, resolved
at `litellm` proxy *startup* time from an environment variable (`ALGENTA_MCP_URL` by default) --
never a literal, hardcoded, or Algenta-hosted endpoint. `lint_mcp_server_entry` flags a URL that
looks Algenta-hosted (`algenta.ai`) even in a config you wrote by hand.

## Quick start

```python
from pathlib import Path
from litellm_algenta.config import build_mcp_server_entry, merge_into_config_file

fragment = build_mcp_server_entry(
    profile="observe",              # default; see Tool profiles below
    server_name="algenta",
    base_url_env_var="ALGENTA_MCP_URL",       # litellm resolves os.environ/ALGENTA_MCP_URL at startup
    authentication_token_env_var="ALGENTA_MCP_TOKEN",
)
merge_into_config_file(Path("config.yaml"), fragment)  # idempotent -- re-run-safe
```

```bash
export ALGENTA_MCP_URL="http://localhost:8000/mcp"     # your own self-hosted engine
export ALGENTA_MCP_TOKEN="..."                          # your own engine's bearer credential
litellm --config config.yaml --port 4000
```

Or skip the Python API and start from a checked-in template directly:

```bash
cp configs/observe.yaml config.yaml   # or govern.yaml / execute.yaml / full.yaml
export ALGENTA_MCP_URL="http://localhost:8000/mcp"
export ALGENTA_MCP_TOKEN="..."
litellm --config config.yaml --port 4000
```

Or use the bundled command-line interface, which does the same thing as the Python snippet above
without writing any Python:

```bash
python -m litellm_algenta.config --profile observe --server-name algenta --merge-into config.yaml
```

```
merged mcp_servers.algenta (observe profile) into config.yaml
```

Leave off `--merge-into` to print the generated YAML to stdout instead of writing it to a file, or
run `python -m litellm_algenta.config --help` for the full flag list (`--base-url-env-var`,
`--auth-type`, `--token-env-var`, `--no-lint`).

Either way, once the gateway is running, a caller talks to it exactly like any other MCP server --
over the native aggregate `/mcp` endpoint (tool names get the server name prefixed, e.g.
`algenta-query_data`) or LiteLLM's own REST convenience API (`GET /mcp-rest/tools/list`,
`POST /mcp-rest/tools/call`) -- this package does not sit on that call path at all once the
gateway is configured; there is nothing left for it to do at request time.

## Try it locally (no engine, no LLM provider key required)

Don't have a self-hosted Algenta Engine running yet? [`examples/try_it_locally.py`](./examples/try_it_locally.py)
is the closest thing to a zero-setup demo. It starts a real `litellm` proxy in front of a real
(but fake-data) stub Algenta MCP server -- the same fixtures this package's own test suite uses
(`tests/stub_server.py`, `tests/proxy_fixture.py`) -- and drives both over real HTTP, so you can
see the profile enforcement actually work before you have an engine or an LLM provider key:

```bash
cd python
uv sync --all-packages --all-extras   # installs the dev extras: litellm[proxy], fastmcp, httpx
uv run python litellm-algenta/examples/try_it_locally.py
```

Real output (trimmed):

```
Tools visible under the 'observe' profile: ['get_contract', 'query_data', 'recommend', 'simulate']
query_data(dataset='demo') -> {"dataset": "demo", "rows": [{"value": 1}, {"value": 2}]}
Calling execute_decision anyway (bypassing the filtered tool list) -> HTTP 403 (refused by the gateway itself, not this package)
```

It stops there -- there is no real engine or LLM in this loop, so it cannot run an actual chat
completion. Point `ALGENTA_MCP_URL` at your own running engine (Quick start, above) to go further.

## Tool profiles

| Profile | `allowed_tools` | Notes |
|---|---|---|
| `observe` (default) | `get_contract`, `query_data`, `simulate`, `recommend` | Read-only. |
| `govern` | + `plan_decision`, `log_decision` | Propose/record decisions; never executes. |
| `execute` | + `execute_decision` | Real-world execution; `allowed_params` restricts its arguments (see below). |
| `full` | *(key omitted -- LiteLLM's own meaning of "unrestricted")* | Opt-in only; admin/ops tooling; still gets the `allowed_params` scrub. |

```python
build_mcp_server_entry(profile="execute", server_name="algenta")
```

## Config templates

[`configs/`](./configs) has one ready-to-use fragment per profile
(`observe.yaml`/`govern.yaml`/`execute.yaml`/`full.yaml`), plus
`observe.oauth2-client-credentials.yaml` demonstrating the OAuth2 machine-to-machine auth type.
Every one of them is generated by `scripts/render_configs.py` calling
`build_mcp_server_entry`/`render_yaml` directly -- `tests/test_configs_up_to_date.py` fails CI if
they ever drift from what those functions currently produce, so they're never hand-edited out of
sync with the code that generates them.

## How this maps onto LiteLLM's real gateway enforcement

Everything below is **verified against a real running `litellm --config ...` proxy (1.98.0)** --
source-cited in `litellm_algenta/config.py`'s module docstring, and re-checked as an executable
test in `tests/test_gateway_conformance.py` -- not assumed from documentation.

**`allowed_tools` is a real, call-time 403 gate, not just a discovery-listing filter.** Calling a
tool outside a server's `allowed_tools` is refused by the gateway itself
(`MCPServerManager.check_allowed_or_banned_tools`) even if a caller bypasses the filtered
`tools/list` response entirely. This package uses it as the *primary* mechanism for profile
enforcement -- materially stronger than a client-side scrub, and it satisfies the contract's own
requirement that `execute`-tier gating be enforced server-side, not by the calling application.

**`allowed_params.execute_decision` is a real, call-time 403 gate on *arguments*.** Confirmed by
reading `MCPServerManager.validate_allowed_params` (called before the upstream request is ever
made) and by a live proxy run: an `execute_decision` call carrying `force: true` against a server
configured with `allowed_params: {execute_decision: ["decision_id", "webhook_url", "timeout_seconds", "metadata"]}`
gets a real `403` -- the argument never reaches the upstream engine. `build_mcp_server_entry` sets
this **unconditionally on every profile under which `execute_decision` is reachable, including
`full`** -- the contract's `never_model_facing_note` says `force`/`override_safety` may never be
model-reachable "in ANY profile, ever," and `full`'s opt-in breadth doesn't get an exception from
that one rule.

**`os.environ/VAR_NAME` genuinely resolves for `mcp_servers:` fields**, including `url` and
`authentication_token` -- confirmed live: a config carrying
`authentication_token: "os.environ/ALGENTA_MCP_TOKEN"` reaches the upstream engine as
`Authorization: Bearer <the real env var's value>`, and the caller's own gateway-facing master
key never reaches the upstream at all.

### What LiteLLM's gateway config genuinely CANNOT enforce -- said plainly

- **It cannot strip `force`/`override_safety` from the *advertised* JSON Schema.**
  `allowed_params` gates arguments actually sent, not what `tools/list` *shows* as available on
  `execute_decision`'s schema -- confirmed by reading the discovery code path, which never
  consults `allowed_params` at all. If your connected Algenta engine's own MCP tool registry
  advertises `force` on `execute_decision`'s schema (the contract says it may, for
  operator/break-glass use), a model talking through this gateway will still *see* `force` listed
  as a settable parameter -- it just cannot make that argument take effect, because
  `allowed_params` 403s the call before it reaches the engine. Getting the schema itself clean is
  **the connected engine's job** (serve a model-facing schema that never advertises these fields),
  not something any `mcp_servers:` config key can do -- unlike `pydantic-ai-algenta` /
  `langchain-algenta`, which strip the schema themselves because their frameworks hand them the
  schema to edit before the model ever sees it. A LiteLLM-fronted deployment doesn't have that
  seam; this package does not pretend otherwise.
- **There is no approval-pause primitive, and the real tool never needs one.** `execute_decision`
  is synchronous: a call either comes back `200` with a real `ExecutionReceipt`
  (`decision_id`/`webhook_url`/`execution_status`/`response_code`/`executed_at`/
  `policy_snapshot_id`/`schema_snapshot_id`/`manifest_version`/`payload_summary`/
  `safety_overridden`) or is refused outright, in that same call, with exactly one of three real
  named gates -- `"idempotency"` (bypassable only via `force=true`, for one re-execution),
  `"confidence"`, or `"risk_floor"` (both bypassable only via `override_safety=true`). There is no
  `pending`/`rejected`/`expired` state to come back and poll later -- a denial and a receipt are
  both final, in the same call that produced them. A JSON-RPC `tools/call` result carries no HTTP
  status of its own, so the only way that denial can reach a caller over MCP is the same way any
  other tool-body exception does: `isError: true`, with the gate name/code/message/override_hint
  inside the result content -- confirmed to pass through this gateway completely unchanged, exactly
  like the already-verified static-credential-401 case above (see
  `tests/test_gateway_conformance.py::test_execute_decision_gate_denial_surfaces_as_iserror`). A
  successful receipt passes through just as unchanged, `isError: false`. There is no LiteLLM-native
  equivalent of `pydantic_ai_algenta`'s `ApprovalRequired` or `langchain_algenta`'s
  `interrupt()`-based pause, and this tool never has anything for one of those to pause on.
  Whatever built the chat-completion request that triggered the tool call is entirely on its own
  to notice `isError` and read the result -- there is no config key, callback, or webhook this
  package could wire up that would change that, because the gateway is receipt-blind by design (it
  has no opinion on what's inside a tool's JSON result).
- **Response headers do not round-trip.** `extra_headers` (see below) forwards a caller-sent
  header to the upstream engine, one direction only -- the gateway's own HTTP response back to
  the caller never carries anything the upstream returned as a response header. Any upstream
  signal that needs to reach the caller has to travel inside the JSON receipt body instead --
  correlate by the receipt's own `decision_id` echo-back.

### Trace headers

`extra_headers: ["x-trace-id"]` (the default `build_mcp_server_entry` sets) forwards that header
from the model-calling client to the upstream Algenta engine, verbatim -- real and verified, not
just documented (`tests/test_gateway_conformance.py`'s trace-header assertions). Without it
configured, the header is silently dropped, never reaching the upstream at all -- also verified,
in the same test, against a second server entry with `extra_headers=()`.

### Auth types and token-expiry behavior

`auth_type` on an `mcp_servers.<name>` entry controls how the *gateway* authenticates to your
*upstream engine* -- a completely separate plane from `general_settings.master_key` /
virtual keys, which control how *callers* authenticate to the *gateway*.

| `auth_type` | What happens on a mid-session upstream 401 |
|---|---|
| `bearer_token` / `api_key` / `basic` / `token` (static credential) | Swallowed into an MCP `isError: true` result carrying the raw stringified exception. **No retry, no reauth** -- verified live (`tests/test_gateway_conformance.py::test_static_bearer_token_401_becomes_iserror_without_retry`). The gateway's own outer HTTP status stays `200` either way; a caller has to notice `isError: true` and parse the exception string itself. |
| `oauth2` (`client_credentials` or `authorization_code`) / `oauth2_token_exchange` / `oauth2_id_jag` | LiteLLM mints/refreshes the upstream token itself; a real invalidate-and-retry-once path exists in source for the token-exchange/OBO modes (not independently reproduced end-to-end here -- it needs a real token-exchange IdP to stand up). `oauth2` requires an explicit `oauth2_flow`; `build_mcp_server_entry` raises `ConfigError` if you omit it, matching a real `litellm` proxy startup crash with the identical requirement (`tests/test_gateway_conformance.py::test_oauth2_without_flow_fails_fast_at_proxy_startup` reproduces that crash directly, independent of this package's own pre-check). |
| `true_passthrough` / `oauth_delegate` | Forwards the caller's own upstream OAuth token unchanged -- LiteLLM mints nothing. The **only** mode where a 401 is relayed to the caller as a clean, real `401` + `WWW-Authenticate` instead of being swallowed, so the caller's own MCP client can redo its OAuth flow. |

Use a static credential only for an engine behind a token you rotate out-of-band and are prepared
to have swallow silently on expiry; use `oauth2`/`oauth2_token_exchange`/`true_passthrough` if you
need a clean, retryable failure signal on expiry instead.

## Why no `algenta-sdk` dependency?

Every other package in this repository may depend on at most one Algenta-owned thing:
[`algenta-sdk`](https://pypi.org/project/algenta-sdk/), a thin HTTP/gRPC client. This package
depends on it for nothing, because there's nothing here that would call it: `algenta-sdk`'s own
`DEFAULT_BASE_URL`/`MCP_ENDPOINT` constants point at Algenta's **hosted cloud**, the opposite of
this repository's self-hosted-only default (see the `typescript/algenta-tools` sibling's README
for the identical reasoning, reaching the identical conclusion) -- and beyond those constants,
`algenta-sdk` has nothing this package's job (generating and linting a YAML config fragment) would
use at all. Declaring it as an unused, leftover-placeholder dependency was exactly the bug an
earlier package's adversarial review caught and fixed; this package simply never adds it.

## Why config generation instead of a Python wrapper class/function?

Because there is nothing to wrap. `pydantic-ai-algenta` wraps `pydantic_ai.mcp.MCPToolset`;
`langchain-algenta` wraps `langchain_mcp_adapters.client.MultiServerMCPClient`; `algenta-tools`
(Vercel AI SDK) wraps `@ai-sdk/mcp`. All three sit *between* the model-calling framework and the
MCP transport, in the same process as the agent. LiteLLM's MCP Gateway sits *outside* every
process that calls it -- it's what those frameworks (or any HTTP client) would call *through*, not
a library any of them import. The honest shape of "integrating a proxy" is generating and
validating the proxy's own config, and testing that config against the real proxy -- not
inventing a Python class this architecture has no room for.

## Why a linter that runs on hand-written configs too?

`lint_mcp_server_entry`/`assert_safe` don't require you to have built your config with
`build_mcp_server_entry` -- they take any `{"mcp_servers": {...}}`-shaped dict. An operator who
starts from `configs/full.yaml` and then hand-adds a second server, or edits `allowed_tools` back
open on an existing one, can still run the linter against the result before starting `litellm`.
The one rule this package treats as non-negotiable regardless of how a config was produced --
`execute_decision`, wherever it's reachable, must have a matching `allowed_params` scrub that
excludes `force`/`override_safety` -- is enforced by re-deriving it from the config's own shape
every time, not by trusting that whoever wrote the config used this package's generator.

## Testing this package

The conformance suite (`tests/test_gateway_conformance.py`) starts a **real** `litellm` proxy
subprocess (via the `litellm` console script installed by the `dev` extra's `litellm[proxy]`)
against a **real** stub Algenta MCP server (`tests/stub_server.py`, a real `fastmcp.FastMCP`
server on a real HTTP socket) and drives it over real HTTP, asserting against the gateway's
actual `/mcp-rest/tools/list` / `/mcp-rest/tools/call` responses -- never a mocked litellm and
never a mocked upstream. This is structurally heavier than the `pydantic-ai-algenta` /
`langchain-algenta` siblings' test suites (they exercise their own in-process wrapper code against
a stub; this package has no in-process wrapper, so its tests spawn the real external gateway
process too) -- proxy startup takes a few seconds per test that needs one, budgeted into three
test functions rather than one-fixture-per-assertion.

```bash
cd python
uv sync --all-packages --all-extras
uv run pytest litellm-algenta -v
```

`tests/test_config_builder.py` and `tests/test_configs_up_to_date.py` are pure unit tests (no
subprocess, no network) covering `config.py`'s own generation/lint/merge logic;
`tests/test_contract_parity.py` loads the real `contracts/integration-tool-contract.json` from
disk (skipped, not failed, outside a repo checkout) and asserts this package's embedded
`contract.py` constants agree with it byte-for-byte on every tool-name set.
