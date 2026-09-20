import type {
	IDataObject,
	IExecuteFunctions,
	INodeExecutionData,
	INodeType,
	INodeTypeDescription,
} from 'n8n-workflow';
import { NodeApiError, NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';

import {
	DEFAULT_PROFILE,
	EXECUTE_DECISION,
	GET_CONTRACT,
	LOG_DECISION,
	PLAN_DECISION,
	QUERY_DATA,
	RECOMMEND,
	SIMULATE,
	isToolAllowedForProfile,
	isToolProfile,
	type ToolProfile,
} from '../../src/contract';
import { connectAlgentaMcp, McpToolError } from '../../src/mcp-client';
import { parseExecutionBlockedBody, parseExecutionReceipt } from '../../src/receipts';

/**
 * Operations whose argument shape is discovered from the connected engine's own MCP `tools/list`
 * response, not fixed here -- `query_data`, `simulate`, `recommend`, `plan_decision` and
 * `log_decision` all take freeform, engine-version-dependent bodies (see
 * `contracts/integration-tool-contract.json`'s `mcp_tool_reference`, which documents what each
 * tool DOES but not a fixed argument schema, and `algenta-tools/src/toolset.ts`'s own comment on
 * why this package discovers schemas at runtime instead of guessing them). Hardcoding typed n8n
 * fields for these would be inventing a schema no other package in this repository assumes.
 * `execute_decision` is the one exception: its schema is unambiguously a single required
 * `decision_id` string (from a prior `log_decision` call), so it gets a real typed field.
 */
const JSON_ARGS_OPERATIONS = new Set([QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION]);

export class Algenta implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'Algenta',
		name: 'algenta',
		icon: { light: 'file:algenta.svg', dark: 'file:algenta.dark.svg' },
		group: ['transform'],
		version: 1,
		subtitle: '={{$parameter["operation"]}}',
		description:
			"Call your own self-hosted Algenta engine's governed-execution MCP tools (query, simulate, recommend, plan, log, and execute a decision)",
		defaults: { name: 'Algenta' },
		inputs: [NodeConnectionTypes.Main],
		outputs: [NodeConnectionTypes.Main],
		usableAsTool: true,
		credentials: [{ name: 'algentaApi', required: true }],
		properties: [
			{
				displayName: 'Operation',
				name: 'operation',
				type: 'options',
				noDataExpression: true,
				default: GET_CONTRACT,
				options: [
					{
						name: 'Get Contract',
						value: GET_CONTRACT,
						description: 'Fetch the live capability/discovery contract for the connected engine',
						action: 'Get the engine capability contract',
					},
					{
						name: 'Query Data',
						value: QUERY_DATA,
						description: 'Run one governed exact query against an authorized, connected dataset',
						action: 'Query governed data',
					},
					{
						name: 'Simulate',
						value: SIMULATE,
						description: 'Run a Monte Carlo / decision simulation over a scenario definition',
						action: 'Simulate a decision scenario',
					},
					{
						name: 'Recommend',
						value: RECOMMEND,
						description: 'Return a governed recommendation without a full simulation run',
						action: 'Recommend for a decision scenario',
					},
					{
						name: 'Plan Decision',
						value: PLAN_DECISION,
						description: 'Produce a structured decision-plan summary for human/governance review',
						action: 'Plan a decision',
					},
					{
						name: 'Log Decision',
						value: LOG_DECISION,
						description: 'Persist a decision record to decision memory and return a decision ID',
						action: 'Log a decision',
					},
					{
						name: 'Execute Decision',
						value: EXECUTE_DECISION,
						description:
							'Dispatch one logged decision to a webhook for real-world execution and return the execution receipt (safety-critical)',
						action: 'Execute a logged decision',
					},
				],
			},
			{
				displayName: 'Decision ID',
				name: 'decisionId',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { operation: [EXECUTE_DECISION] } },
				description: 'The decision_id returned by a prior Log Decision call',
			},
			{
				displayName: 'Webhook URL',
				name: 'webhookUrl',
				type: 'string',
				default: '',
				required: true,
				displayOptions: { show: { operation: [EXECUTE_DECISION] } },
				description: 'Where the connected engine delivers this decision for real-world execution',
			},
			{
				displayName: 'Arguments',
				name: 'arguments',
				type: 'json',
				default: '{}',
				displayOptions: {
					show: {
						operation: [QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION],
					},
				},
				description:
					"This tool's argument shape is discovered from the connected engine's own MCP contract at call time and can vary by engine version — call Get Contract first, or see docs.algenta.ai, for the current shape",
			},
			{
				displayName: 'Tool Profile',
				name: 'profile',
				type: 'options',
				default: DEFAULT_PROFILE,
				options: [
					{ name: 'Observe (Read Only)', value: 'observe' },
					{ name: 'Govern (Adds Plan and Log)', value: 'govern' },
					{ name: 'Execute (Adds Execute Decision)', value: 'execute' },
					{ name: 'Full (Opt-In, Entire Registry)', value: 'full' },
				],
				description:
					"Refuses an operation the selected profile does not allow, matching contracts/integration-tool-contract.json. Relevant even outside an AI Agent: it is the same guard that stops an autonomous caller from reaching Execute Decision through this node.",
			},
		],
	};

	async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
		const items = this.getInputData();
		const returnData: INodeExecutionData[] = [];

		const credentials = await this.getCredentials('algentaApi');
		const baseUrl = credentials.baseUrl as string;
		const apiKey = (credentials.apiKey as string) ?? '';

		const connection = await connectAlgentaMcp(baseUrl, apiKey);
		try {
			for (let itemIndex = 0; itemIndex < items.length; itemIndex++) {
				try {
					const operation = this.getNodeParameter('operation', itemIndex) as string;
					const profileParam = this.getNodeParameter('profile', itemIndex, DEFAULT_PROFILE) as string;
					const profile: ToolProfile = isToolProfile(profileParam) ? profileParam : DEFAULT_PROFILE;

					// Enforced against the tool NAME the node is about to call, not against whatever the
					// connected engine advertises right now -- the same fixed contract boundary every
					// sibling package in this repo enforces, so a profile means the same thing everywhere.
					if (!isToolAllowedForProfile(operation, profile, new Set([operation]))) {
						throw new NodeOperationError(
							this.getNode(),
							`Operation '${operation}' is not allowed under the '${profile}' tool profile`,
							{ itemIndex },
						);
					}

					const args = buildArgs(this, operation, itemIndex);
					const raw = await connection.callTool(operation, args);

					if (operation === EXECUTE_DECISION) {
						const receipt = parseExecutionReceipt(raw);
						if (receipt) {
							returnData.push({ json: receipt as unknown as IDataObject, pairedItem: itemIndex });
							continue;
						}
					}

					returnData.push({ json: normalizeJson(raw), pairedItem: itemIndex });
				} catch (error) {
					if (error instanceof McpToolError && this.getNodeParameter('operation', itemIndex) === EXECUTE_DECISION) {
						const blocked = parseExecutionBlockedBody(safeJsonParse(error.detail));
						if (blocked) {
							const apiError = new NodeApiError(
								this.getNode(),
								{ code: blocked.code, gate: blocked.gate, message: blocked.message },
								{
									message: `Blocked by policy (gate: ${blocked.gate})`,
									description: blocked.message,
								},
							);
							if (this.continueOnFail()) {
								returnData.push({
									json: { error: apiError.message, gate: blocked.gate, code: blocked.code },
									pairedItem: itemIndex,
								});
								continue;
							}
							throw apiError;
						}
					}
					if (this.continueOnFail()) {
						returnData.push({ json: { error: (error as Error).message }, pairedItem: itemIndex });
						continue;
					}
					if (error instanceof NodeOperationError || error instanceof NodeApiError) {
						// Already a real, well-typed n8n error (re-thrown as-is, not wrapped again) --
						// the lint rule below can't see through the instanceof guard on this line alone.
						// eslint-disable-next-line @n8n/community-nodes/require-node-api-error
						throw error;
					}
					throw new NodeOperationError(this.getNode(), error as Error, { itemIndex });
				}
			}
		} finally {
			await connection.close();
		}

		return [returnData];
	}
}

function buildArgs(ctx: IExecuteFunctions, operation: string, itemIndex: number): Record<string, unknown> {
	if (operation === EXECUTE_DECISION) {
		const decisionId = ctx.getNodeParameter('decisionId', itemIndex) as string;
		const webhookUrl = ctx.getNodeParameter('webhookUrl', itemIndex) as string;
		return { decision_id: decisionId, webhook_url: webhookUrl };
	}
	if (operation === GET_CONTRACT) {
		return {};
	}
	if (JSON_ARGS_OPERATIONS.has(operation)) {
		const raw = ctx.getNodeParameter('arguments', itemIndex, '{}');
		return normalizeArgsParam(ctx, raw, itemIndex);
	}
	return {};
}

function normalizeArgsParam(ctx: IExecuteFunctions, raw: unknown, itemIndex: number): Record<string, unknown> {
	if (typeof raw === 'object' && raw !== null) {
		return raw as Record<string, unknown>;
	}
	if (typeof raw === 'string') {
		const trimmed = raw.trim();
		if (trimmed === '') {
			return {};
		}
		let parsed: unknown;
		try {
			parsed = JSON.parse(trimmed);
		} catch (error) {
			throw new NodeOperationError(ctx.getNode(), error as Error, {
				itemIndex,
				description: 'Arguments must be valid JSON',
			});
		}
		if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
			return parsed as Record<string, unknown>;
		}
		throw new NodeOperationError(ctx.getNode(), 'Arguments must be a JSON object', { itemIndex });
	}
	return {};
}

function normalizeJson(raw: unknown): IDataObject {
	if (typeof raw === 'object' && raw !== null && !Array.isArray(raw)) {
		return raw as IDataObject;
	}
	return { result: raw } as IDataObject;
}

function safeJsonParse(text: string): unknown {
	try {
		return JSON.parse(text);
	} catch {
		return null;
	}
}
