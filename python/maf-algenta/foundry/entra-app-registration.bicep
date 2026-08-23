// entra-app-registration.bicep
//
// NOT INDEPENDENTLY VERIFIED AGAINST A LIVE FOUNDRY PROJECT. Read foundry/README.md's opening
// section before applying this. It is transcribed accurately against current Microsoft Learn
// documentation (see the citation table in foundry/README.md for exact URLs and quotes) and was
// never deployed against a real Azure subscription, Entra tenant, or Foundry project in this
// environment -- there isn't one available here.
//
// WHAT THIS DOES
// Registers a Microsoft Entra application (and its service principal) to act as the "resource"
// / audience for Algenta's self-hosted MCP endpoint, satisfying the one hard requirement Foundry
// imposes on a custom OAuth MCP connection -- quoted verbatim from Microsoft's own current docs
// (learn.microsoft.com/.../mcp-authentication, ms.date 2026-08-11):
//
//   "Agent Service restricts tokens scoped to a known Microsoft audience from being sent to
//   custom or third-party MCP servers ... Your custom MCP server must be registered with an
//   audience that you control rather than a known Microsoft audience."
//
// This app registration's `identifierUris` value (the App ID URI / audience) is that
// self-owned audience. Algenta's own MCP server is responsible for validating that incoming
// bearer tokens carry this audience (the `aud` claim) -- this template cannot make Algenta's
// server do that; it only provisions the Entra side.
//
// WHAT THIS DOES NOT DO
// - Does not create a client secret with a retrievable value. `passwordCredentials.secretText`
//   on `Microsoft.Graph/applications` is populated by Microsoft Entra ID only on the initial
//   POST that creates the credential and is never retrievable again afterwards (documented
//   read-only behavior, not a limitation of this template) -- so a plaintext secret cannot
//   round-trip through a declarative, re-appliable Bicep file at all. Create the client secret
//   as a separate, one-time, owner-run step (`az ad app credential reset --id <appId>` or the
//   Entra ID portal) *after* this template deploys, then pass it to
//   `azd ai connection create ... --client-secret <value>` (see README) -- never commit it
//   anywhere, including back into this file.
// - Does not create the Foundry project connection, the toolbox, or the agent's `mcp` tool
//   config -- those are Foundry-side resources reached via `azd ai connection create` /
//   `azd ai toolbox create` (real commands, quoted in README), not Microsoft Graph or ARM
//   resources this template's scope can reach.
// - Does not set the OAuth app's redirect URI to its final value. Foundry hands back a redirect
//   URL only *after* `azd ai connection create --auth-type oauth2` succeeds against an app that
//   already exists -- a real circular dependency the docs surface as a genuine two-pass flow
//   ("If you use custom OAuth, you receive a redirect URL after configuration. Add the redirect
//   URL to your OAuth app."). This template's `oauthRedirectUri` parameter defaults to a
//   placeholder for that reason; re-run this deployment (or `az ad app update`) once you have
//   the real value.
//
// PREREQUISITES (per Microsoft's own current docs, see README citation table)
// - Bicep CLI v0.36.1+ (`az bicep upgrade`), Azure CLI 2.73.0+.
// - `bicepconfig.json` next to this file declaring the Microsoft Graph Bicep extension (provided
//   alongside this file in this same directory -- see bicepconfig.json).
// - The deploying identity needs `Application.ReadWrite.OwnedBy` (least privileged) or
//   `Application.ReadWrite.All` on Microsoft Graph to create `Microsoft.Graph/applications`,
//   and the same permission tier again for `Microsoft.Graph/servicePrincipals` -- both quoted
//   directly from the Microsoft Graph Bicep v1.0 reference pages for these two resource types.

extension microsoftGraphV1

@description('''
Display name for the Entra app registration that represents Algenta's self-hosted MCP endpoint
as a Foundry-consumable resource/audience.
''')
param appDisplayName string = 'Algenta MCP Server (self-hosted)'

@description('''
The self-owned App ID URI / audience for this app. MUST be a URI you control -- never a
Microsoft-owned audience (e.g. never "https://ai.azure.com", never anything under a Microsoft
first-party domain) -- per the exact "Agent Service restricts tokens scoped to a known Microsoft
audience..." requirement quoted in this file's header and in README.md. The default below uses
the `api://<tenantId>/<appIdUriSuffix>` form Microsoft's own app-registration guidance recommends
for single-tenant apps to guarantee global uniqueness without needing a verified custom domain.
''')
param audienceAppIdUriSuffix string = 'algenta-mcp'

@description('Restrict sign-in to this tenant only. AzureADMyOrg is the least-privileged default for a resource app that only Foundry, in your own tenant, should ever request tokens for.')
@allowed(['AzureADMyOrg', 'AzureADMultipleOrgs'])
param signInAudience string = 'AzureADMyOrg'

@description('''
Placeholder redirect URI for the custom-OAuth app. Foundry only returns the real value after
`azd ai connection create --auth-type oauth2` succeeds against this app registration (see this
file's header comment and README.md's "two-pass flow" note) -- update and redeploy (or
`az ad app update --id <appId> --web-redirect-uris <real-url>`) once you have it.
''')
param oauthRedirectUri string = 'https://REPLACE-AFTER-azd-ai-connection-create.example.invalid/callback'

@description('''
Whether to also define three appRoles ("Mcp.Observe", "Mcp.Govern", "Mcp.Execute") mirroring
this repository's own tool-profile contract (contracts/integration-tool-contract.json), so
Algenta's own MCP server can authorize a caller by checking the Entra access token's `roles`
claim against these values -- the same shape as any custom (non-Azure-native) resource
implementing app-role-based authorization against its own Entra app registration. This is this
template's design choice for satisfying the contract's execute profile's "explicit server-side
enablement" and "caller role/permission check" gates in an Entra-native way; it is NOT something
Microsoft's docs prescribe for MCP specifically -- Algenta's own server is responsible for
actually checking these roles, which this template cannot verify or enforce on your MCP server
implementation for you. See README.md's "Why appRoles, and what it does and doesn't give you"
section.
''')
param defineContractAppRoles bool = true

@description('''
Immutable alternate key for this application resource (required by Microsoft.Graph/applications
-- see the property table in the Microsoft Graph Bicep v1.0 reference for `applications`).
Deliberately derived from `audienceAppIdUriSuffix` rather than left free-form, so re-running this
deployment against the same intended audience updates the same application instead of creating a
second one under a different key.
''')
param uniqueName string = 'algenta-mcp-server-${audienceAppIdUriSuffix}'

var audienceAppIdUri = 'api://${tenant().tenantId}/${audienceAppIdUriSuffix}'

var contractAppRoles = defineContractAppRoles ? [
  {
    id: guid(audienceAppIdUri, 'Mcp.Govern')
    allowedMemberTypes: ['Application']
    displayName: 'Algenta MCP - govern'
    description: 'Caller may call plan_decision/log_decision in addition to the observe-tier tools (contracts/integration-tool-contract.json govern profile). Does not by itself authorize execute_decision.'
    value: 'Mcp.Govern'
    isEnabled: true
  }
  {
    id: guid(audienceAppIdUri, 'Mcp.Execute')
    allowedMemberTypes: ['Application']
    displayName: 'Algenta MCP - execute'
    description: 'Caller may call execute_decision. Per contracts/integration-tool-contract.json, this app role being present is only ONE of six required gates (requires_all_of) -- it is not sufficient on its own, and Algenta\'s own server, not Entra, enforces the rest (out-of-band policy approval, idempotency key, plan_hash of an already-planned+logged decision, single-use replay nonce).'
    value: 'Mcp.Execute'
    isEnabled: true
  }
] : []

resource mcpResourceApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: uniqueName
  displayName: appDisplayName
  signInAudience: signInAudience
  identifierUris: [
    audienceAppIdUri
  ]
  web: {
    redirectUris: [
      oauthRedirectUri
    ]
  }
  appRoles: contractAppRoles
}

// The service principal is what Entra actually issues tokens against / what a role assignment
// (an `appRoleAssignedTo`, or an Azure RBAC role if you front this app with an Azure resource)
// targets. `Microsoft.Graph/servicePrincipals.appId` must equal the application's own `appId` --
// quoted directly from the Microsoft Graph Bicep v1.0 reference for servicePrincipals ("The
// unique identifier for the associated application (its appId property). Alternate key.").
resource mcpResourceServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: mcpResourceApp.appId
  appRoleAssignmentRequired: true
}

output applicationObjectId string = mcpResourceApp.id
output applicationClientId string = mcpResourceApp.appId
output audienceAppIdUriValue string = audienceAppIdUri
output servicePrincipalObjectId string = mcpResourceServicePrincipal.id
output govern_appRoleId string = defineContractAppRoles ? guid(audienceAppIdUri, 'Mcp.Govern') : ''
output execute_appRoleId string = defineContractAppRoles ? guid(audienceAppIdUri, 'Mcp.Execute') : ''
