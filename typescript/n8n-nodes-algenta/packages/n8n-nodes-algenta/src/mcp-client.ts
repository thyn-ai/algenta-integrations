/**
 * Thin wrapper over the OFFICIAL `@modelcontextprotocol/sdk` client -- the same package n8n's
 * own bundled `n8n-nodes-langchain.mcpClient` node uses -- rather than hand-rolling JSON-RPC
 * framing. One short-lived connection per node execution: n8n runs a workflow to completion and
 * tears the process down between invocations, so there is no long-lived session to pool across
 * calls the way a persistent server would.
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

const CLIENT_NAME = 'n8n-nodes-algenta';
const CLIENT_VERSION = '0.1.0';

export interface AlgentaMcpConnection {
	callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
	close(): Promise<void>;
}

/** Connects to `${baseUrl}/mcp` over Streamable HTTP. `apiKey`, when set, is sent as a bearer
 * token on every request the transport opens -- the same header shape the credential's `test`
 * request and every other Algenta integration package's bearer-auth path use. */
export async function connectAlgentaMcp(baseUrl: string, apiKey: string): Promise<AlgentaMcpConnection> {
	const url = new URL('/mcp', baseUrl);
	const transport = new StreamableHTTPClientTransport(url, {
		requestInit: apiKey ? { headers: { Authorization: `Bearer ${apiKey}` } } : undefined,
	});
	const client = new Client({ name: CLIENT_NAME, version: CLIENT_VERSION });
	await client.connect(transport);

	return {
		async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
			const result = await client.callTool({ name, arguments: args });
			// MCP tool errors are IN-BAND (`isError: true` on an otherwise-normal result), not
			// thrown -- surfacing them as a JS throw here lets every caller use one try/catch path
			// for both a transport failure and a tool-level denial (execute_decision's blocked
			// response arrives exactly this way).
			if (result.isError) {
				const detail = Array.isArray(result.content)
					? result.content
							.map((part) => (typeof part === 'object' && part !== null && 'text' in part ? String((part as { text: unknown }).text) : ''))
							.join('\n')
					: '';
				throw new McpToolError(name, detail);
			}
			return extractStructuredOrTextContent(result);
		},
		async close(): Promise<void> {
			await client.close();
		},
	};
}

/** Thrown for an in-band MCP tool-error result. `detail` is the tool's own error text -- for
 * `execute_decision` this is JSON matching `executionBlockedBodySchema` in `receipts.ts`; for
 * every other tool it is a freeform message the node surfaces as-is. */
export class McpToolError extends Error {
	readonly toolName: string;
	readonly detail: string;

	constructor(toolName: string, detail: string) {
		super(`Algenta MCP tool '${toolName}' returned an error: ${detail || '(no detail)'}`);
		this.name = 'McpToolError';
		this.toolName = toolName;
		this.detail = detail;
	}
}

function extractStructuredOrTextContent(result: unknown): unknown {
	// Prefer structuredContent (a tool's typed JSON result) when the server provides it; fall back
	// to parsing the first text content block as JSON, and finally to the raw text. Typed as
	// `unknown` rather than a declared shape: the SDK's real `callTool` return type is a wide
	// union whose branches don't structurally agree on one common object shape, so narrowing at
	// runtime here is more honest than asserting a shape that doesn't always hold.
	if (typeof result !== 'object' || result === null) {
		return null;
	}
	const record = result as Record<string, unknown>;
	if (record.structuredContent !== undefined) {
		return record.structuredContent;
	}
	if (Array.isArray(record.content) && record.content.length > 0) {
		const first = record.content[0] as { type?: string; text?: string };
		if (first?.type === 'text' && typeof first.text === 'string') {
			try {
				return JSON.parse(first.text);
			} catch {
				return first.text;
			}
		}
	}
	return null;
}
