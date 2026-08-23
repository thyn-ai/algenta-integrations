# Foundry registration artifacts for Algenta's self-hosted MCP endpoint

> **This is ready for an owner to apply against their own Microsoft Foundry project. It has NOT
> been independently verified against a live Foundry deployment here.** There is no Azure
> subscription, Entra tenant, or Foundry project available in this environment. Nothing below
> was deployed, nothing was exercised end to end, and no Bicep resource here was ever actually
> created in a real tenant. What *was* done: every command, resource type, property name, and
> quoted requirement below was checked directly against Microsoft's own current Microsoft Learn
> documentation while writing this (see the [citation table](#citation-table) -- exact URLs, exact
> `ms.date`/`updated_at`, exact quotes), and the Bicep template
> ([`entra-app-registration.bicep`](./entra-app-registration.bicep)) was run through the real,
> installed `az bicep build` and `az bicep lint` (Bicep CLI 0.44.1) and compiles cleanly with zero
> errors and zero warnings -- a syntax/schema check, not a deployment, and not a substitute for
> one. Treat this the way this repository's own `litellm-algenta` README treats an
> un-exercised-in-CI deliverable: accurate and usable, not "tested."

## Why this lives under `python/maf-algenta/foundry/`, not a top-level `foundry/`

Kept alongside the package it's paired with rather than promoted to a repository-root directory,
because the root of this repository is reserved for concerns that apply across every package
(`contracts/`, `scripts/`, `.github/`) -- Foundry registration is specific to this one track's
"MAF (buildable) vs. Foundry (hosted, owner-gated)" split, documented in `../README.md`'s own
opening section. If a second package in this repository ever needs the same kind of Foundry
artifact, promoting this to a shared top-level `foundry/` at that point is the right call; one
consumer doesn't justify it yet.

## What's here

| File | What it is |
|---|---|
| [`entra-app-registration.bicep`](./entra-app-registration.bicep) | Registers a Microsoft Entra application + service principal to act as the self-owned audience/resource for Algenta's MCP endpoint, with optional `appRoles` mirroring this repository's tool-profile contract. |
| [`bicepconfig.json`](./bicepconfig.json) | Declares the Microsoft Graph Bicep extension (`microsoftGraphV1`) the template above depends on -- must sit next to the `.bicep` file. |

Deliberately **not** here: a Foundry project connection, toolbox, or agent `mcp` tool
definition -- those are Foundry-side objects created via the `azd ai` CLI (real commands, quoted
below), not ARM or Microsoft Graph resources a Bicep template can reach.

## The one hard requirement: a self-owned audience

Quoted verbatim from [Set up MCP server authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication)
(`ms.date: 2026-08-11`, `updated_at: 2026-08-13`):

> "Agent Service restricts tokens scoped to a known Microsoft audience from being sent to custom
> or third-party MCP servers ... Your custom MCP server must be registered with an audience that
> you control rather than a known Microsoft audience. Don't design your MCP server to rely on
> passthrough of its authentication token to a downstream Microsoft service. To meet this
> requirement, use custom OAuth with your own Microsoft Entra app registration."

`entra-app-registration.bicep` exists to satisfy exactly this: its `identifierUris` (the App ID
URI) defaults to `api://<your-tenant-id>/algenta-mcp`, a self-owned audience under your own
tenant, never a Microsoft-owned one. **Algenta's own MCP server is responsible for actually
validating that an incoming bearer token's `aud` claim equals this value** -- this template
provisions the Entra side only; it cannot make your server check anything.

## Apply the Bicep template

```bash
az bicep upgrade   # confirm you're on v0.36.1+ (Microsoft's stated minimum for these resource types)
az login
az deployment sub create \
  --location <your-region> \
  --template-file entra-app-registration.bicep \
  --parameters audienceAppIdUriSuffix=algenta-mcp appDisplayName="Algenta MCP Server (prod)"
```

Requires `Application.ReadWrite.OwnedBy` (least privileged) or `Application.ReadWrite.All` on
Microsoft Graph for the deploying identity -- quoted directly from the Microsoft Graph Bicep
v1.0 reference pages for both `Microsoft.Graph/applications` and `Microsoft.Graph/servicePrincipals`.

Two things this template deliberately cannot finish for you, both documented in the template's
own header comment:

1. **The client secret.** `passwordCredentials.secretText` is populated by Entra only on the
   initial creation call and is never retrievable again -- a plaintext secret cannot round-trip
   through a re-appliable declarative template. Create it as a separate step:
   ```bash
   az ad app credential reset --id <applicationClientId-output-above>
   ```
2. **The real OAuth redirect URI.** Foundry hands this back only *after* `azd ai connection
   create --auth-type oauth2` succeeds against an app that already exists -- documented directly:
   "If you use custom OAuth, you receive a redirect URL after configuration. Add the redirect URL
   to your OAuth app so Agent Service can complete the flow." Re-run the deployment with the real
   `oauthRedirectUri` value (or `az ad app update --id <appId> --web-redirect-uris <url>`) once
   you have it.

## Register the MCP endpoint with Foundry (`azd ai` CLI)

All six command forms below are quoted verbatim (only the placeholder values are ours) from
[Connect agents to MCP server endpoints](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol)
(`ms.date: 2026-08-05`, `updated_at: 2026-08-19`):

```bash
PROJECT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>"
azd ai project set $PROJECT_ENDPOINT

# oauth2 -- the variant this template's app registration is for
azd ai connection create algenta-mcp-conn \
  --kind remote-tool \
  --target https://<your-algenta-base-url>/mcp \
  --auth-type oauth2 \
  --authorization-url https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/authorize \
  --token-url https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token \
  --client-id <applicationClientId-output-above> \
  --client-secret <the-secret-from-az-ad-app-credential-reset> \
  --scopes "api://<tenant-id>/algenta-mcp/.default"
```

The full `--auth-type` matrix, for the other five shapes an operator might genuinely want instead
of `oauth2` (quoted from the same page):

| `--auth-type` | Additional flags | When you'd use it instead |
|---|---|---|
| `none` | -- | Algenta's MCP endpoint has no auth of its own (development only). |
| `custom-keys` | `--custom-key "Header=Value"` (repeatable) | A static bearer token / API key instead of full OAuth. |
| `oauth2` | `--authorization-url`, `--token-url`, `--client-id`, `--client-secret`, `--scopes` | Your own Entra app registration (this template's target). |
| `user-entra-token` | `--audience <entra-audience>` | Per-user Entra identity passthrough to Algenta's own resource, if Algenta validates end-user tokens directly. |
| `project-managed-identity` | `--audience <entra-audience>` (optional) | Every agent in the Foundry project shares the project's own managed identity. |
| `agentic-identity` | `--audience <entra-audience>` | Each published agent gets its own distinct identity/role assignments against Algenta's resource. |

For `user-entra-token` / `project-managed-identity` / `agentic-identity`, quoted directly: *"assign
the corresponding principal the required RBAC role on the target resource before you call the
toolbox."* Since Algenta's self-hosted engine isn't itself a native Azure resource with built-in
RBAC, "the target resource" is whatever fronts it (an API Management instance, an Azure Container
Apps ingress with its own auth, or -- the shape this template chooses -- Algenta's own Entra app
registration's `appRoles`, checked by Algenta's own server against the token's `roles` claim; see
[Why `appRoles`](#why-approles-and-what-it-does-and-doesnt-give-you) below).

### Registering it as a Foundry `mcp` tool directly (no Toolbox)

The exact tool JSON, quoted from the same page (Python `MCPTool` shape shown; every language SDK
takes the same three fields):

```python
tool = MCPTool(
    server_label="algenta",
    server_url="https://<your-algenta-base-url>/mcp",
    require_approval="always",  # or {"always": ["execute_decision"]} -- see below
    project_connection_id="algenta-mcp-conn",
)
```

**Set `require_approval` to at least gate `execute_decision`.** Nothing in Foundry's own
`require_approval` config knows about this repository's tool-profile contract -- it is a
per-tool-call approval gate, orthogonal to (and not a substitute for) the contract's own
`execute` profile's `requires_all_of` list. Use the per-tool-list form so read-only tools aren't
needlessly gated:
```python
require_approval={"always": ["execute_decision"]}
```

### Registering it as a Foundry Toolbox instead

Quoted from the same page -- a Toolbox is the reusable form, and is explicitly documented as
MCP-Framework-compatible: *"Because the Toolbox endpoint is MCP-compatible, any runtime that can
consume an MCP server can also consume a Toolbox. This compatibility includes Foundry Agent
Service, **Microsoft Agent Framework**, LangGraph, GitHub Copilot SDK, and other MCP-enabled
clients."* -- meaning `maf-algenta`'s own `create_algenta_tools` (see `../README.md`) is,
per Microsoft's own docs, a legitimate way to consume a Toolbox that happens to include Algenta's
MCP server, not only a way to consume Algenta directly.

```yaml
# my-toolbox.yaml
description: MCP server tools
connections:
  - name: algenta-mcp-conn
```
```bash
azd ai toolbox create my-toolbox --from-file my-toolbox.yaml
```

## Why `appRoles`, and what it does and doesn't give you

`entra-app-registration.bicep` optionally (`defineContractAppRoles`, default `true`) defines two
`appRoles` on the app registration itself -- `Mcp.Govern` and `Mcp.Execute` -- deliberately named
to mirror this repository's own `contracts/integration-tool-contract.json` profile boundary
(`observe` has no role requirement; `govern` and `execute` each get one). This is **this
template's own design choice for satisfying the contract's `execute` profile's "explicit
server-side enablement" and "caller role/permission check" gates in an Entra-native way** -- it
is not something Microsoft's MCP documentation prescribes, and it is not a real RBAC role
assignment on an Azure resource the way `project-managed-identity`/`agentic-identity` role
assignments are, because Algenta's self-hosted engine has no native Azure RBAC surface of its
own. What it *does* give you: a real, standard Entra mechanism (an `appRoleAssignedTo` grant
against this app's own service principal, or a role-assignable claim in a token issued for it)
that Algenta's own server-side authorization code can check the same way any custom (non-Azure)
resource checks the `roles` claim in a validated access token.

**What this cannot do:** verify that Algenta's own MCP server actually enforces any of this. The
contract's `execute` profile requires six things together
(`contracts/integration-tool-contract.json`'s `requires_all_of`), and an Entra app role can only
ever stand in for one of them (a role/permission check) -- the out-of-band policy approval, the
idempotency key, the `plan_hash` linkage to an already-planned-and-logged decision, and the
single-use replay nonce are all Algenta engine-side responsibilities no Entra configuration can
satisfy on the engine's behalf.

## Citation table

Every URL below was fetched and read directly while writing this deliverable (not recalled from
training data). `ms.date` is the page's own stated authoring date; `updated_at` (where present) is
its last edit timestamp -- both included so this can be re-checked for staleness later without
re-doing the research pass, per this repository's own standing practice.

| Claim | Source | `ms.date` / `updated_at` |
|---|---|---|
| "Agent Service restricts tokens scoped to a known Microsoft audience..." / custom-audience requirement; agent identity vs. project managed identity; "Make sure the agent identity has the required role assignments on the underlying service..." | [Set up MCP server authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication) | 2026-08-11 / 2026-08-13 |
| The full `azd ai connection create --auth-type ...` flag matrix (all six variants); the `MCPTool(server_label=, server_url=, require_approval=, project_connection_id=)` shape; `require_approval`'s three forms; the Toolbox-is-MCP-compatible-with-Microsoft-Agent-Framework quote; "The Agent Service runtime only accepts a remote MCP server endpoint..." | [Connect agents to MCP server endpoints](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol) | 2026-08-05 / 2026-08-19 |
| `Microsoft.Graph/applications@v1.0`'s full property list, `uniqueName`/`identifierUris`/`signInAudience`/`web.redirectUris`/`appRoles` shapes and requiredness, least-privileged Graph permissions (`Application.ReadWrite.OwnedBy`) | [Microsoft.Graph/applications reference](https://learn.microsoft.com/en-us/graph/templates/bicep/reference/applications) | 2026-05-13 / 2026-05-13 |
| `Microsoft.Graph/servicePrincipals@v1.0`'s full property list, `appId` being the required alternate key linking it to the application, `appRoleAssignmentRequired` | [Microsoft.Graph/servicePrincipals reference](https://learn.microsoft.com/en-us/graph/templates/bicep/reference/serviceprincipals) | 2026-05-13 / 2026-05-13 |
| The Microsoft Graph Bicep extension's existence/scope/license requirement | [Bicep Templates for Microsoft Graph Resources](https://learn.microsoft.com/en-us/graph/templates/bicep/overview-bicep-templates-for-graph) | 2025-07-28 / 2025-08-11 |
| Bicep/Azure CLI minimum versions (Bicep v0.36.1+, Azure CLI 2.73.0+) | [Set up tools for deploying Microsoft Graph Bicep resource types](https://learn.microsoft.com/en-us/graph/templates/bicep/quickstart-install-bicep-tools) | 2025-07-28 / 2025-08-11 |
| The exact `bicepconfig.json` shape (`"extensions": {"microsoftGraphV1": "br:mcr.microsoft.com/bicep/extensions/microsoftgraph/v1.0:1.0.0"}`) and the `extension microsoftGraphV1` statement | [Create and deploy Microsoft Graph resources with Bicep](https://learn.microsoft.com/en-us/graph/templates/bicep/quickstart-create-bicep-interactive-mode) | 2025-07-28 / 2025-08-11 |

## NOT VERIFIED -- explicit list

None of the following was exercised, in any form, in this environment:

- Deploying `entra-app-registration.bicep` against a real Azure subscription or Entra tenant.
- Creating the app's client secret, or any `az ad app credential reset` call.
- Running any `azd ai connection create`, `azd ai toolbox create`, or `azd ai project set`
  command against a real Foundry project.
- The OAuth consent flow, the `oauth_consent_request` / `mcp_approval_request` response shapes,
  or resuming a paused response after approval.
- RBAC role assignment of any kind, `appRoleAssignedTo` grants, or a token actually being issued
  and validated against this app's audience.
- Private-endpoint/VNet deployment of Algenta's MCP server for Foundry's private-MCP path.
- Azure API Center registration of this MCP server (mentioned in the approved plan's research
  notes as real and current, but out of scope for this template -- a follow-up if the owner wants
  org-wide catalog registration rather than a per-project connection).

Everything above is transcribed accurately from currently-published Microsoft documentation and
schema-checked with the real Bicep compiler. None of it is a substitute for actually running it
against a live Foundry project, which is this deliverable's owner's job, not this repository's.
