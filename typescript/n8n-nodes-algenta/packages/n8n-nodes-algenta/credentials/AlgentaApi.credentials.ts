import type { Icon, ICredentialTestRequest, ICredentialType, INodeProperties } from 'n8n-workflow';

/**
 * Connection details for a self-hosted Algenta engine's MCP endpoint. There is no
 * hosted-by-Algenta option here on purpose -- see the package README's Self-hosted-first section
 * and `contracts/integration-tool-contract.json`'s `self_hosted_only_note`: every tool call this
 * package makes goes to the caller's own engine, over the caller's own network, at request time.
 */
export class AlgentaApi implements ICredentialType {
	name = 'algentaApi';

	displayName = 'Algenta API';

	icon: Icon = { light: 'file:../nodes/Algenta/algenta.svg', dark: 'file:../nodes/Algenta/algenta.dark.svg' };

	documentationUrl = 'https://docs.algenta.ai/sdks/mcp';

	properties: INodeProperties[] = [
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'http://localhost:8000',
			placeholder: 'http://localhost:8000',
			description:
				'The base URL of your own self-hosted Algenta engine. The node appends /mcp itself -- do not include it here.',
		},
		{
			displayName: 'API Key',
			name: 'apiKey',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			description: 'Bearer token for the connected engine, if it requires authentication.',
		},
	];

	// Not an IAuthenticateGeneric header-injection block: this credential authenticates an MCP
	// session (an @modelcontextprotocol/sdk StreamableHTTPClientTransport), not a plain HTTP
	// request the node makes with `this.helpers.httpRequest`. The Algenta node's own execute()
	// reads `apiKey`/`baseUrl` directly from the resolved credential and passes the bearer token
	// to the transport's own `requestInit.headers`, matching how the transport actually attaches
	// auth to every request/SSE-stream it opens -- an IAuthenticateGeneric block only knows how to
	// decorate a single outgoing n8n HTTP request, which is not what's happening here.

	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials.baseUrl}}',
			url: '/v1/health',
			method: 'GET',
		},
	};
}
