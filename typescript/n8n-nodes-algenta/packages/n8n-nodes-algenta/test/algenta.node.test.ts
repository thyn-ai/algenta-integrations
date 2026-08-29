import type { IExecuteFunctions, INode, INodeExecutionData } from 'n8n-workflow';
import { NodeApiError, NodeOperationError } from 'n8n-workflow';
import { afterEach, describe, expect, it } from 'vitest';

import { Algenta } from '../nodes/Algenta/Algenta.node';
import { startStubAlgentaServer, type StubServerHandle } from './test-support/stub-server';

let handle: StubServerHandle | undefined;

afterEach(async () => {
	if (handle) {
		await handle.close();
		handle = undefined;
	}
});

const FAKE_NODE: INode = {
	id: '1',
	name: 'Algenta',
	type: 'n8n-nodes-algenta.algenta',
	typeVersion: 1,
	position: [0, 0],
	parameters: {},
};

/** A minimal fake `IExecuteFunctions`, covering exactly the methods `Algenta.node.ts`'s
 * `execute()` calls. `params[itemIndex]` holds that item's node-parameter values;
 * `continueOnFailValue` toggles the same behavior a real workflow's "Continue on Fail" node
 * setting would. */
function buildContext(options: {
	items: INodeExecutionData[];
	params: Array<Record<string, unknown>>;
	baseUrl: string;
	apiKey?: string;
	continueOnFailValue?: boolean;
}): IExecuteFunctions {
	return {
		getInputData: () => options.items,
		getNodeParameter: (name: string, itemIndex: number, fallback?: unknown) => {
			const value = options.params[itemIndex]?.[name];
			return value === undefined ? fallback : value;
		},
		getCredentials: async () => ({ baseUrl: options.baseUrl, apiKey: options.apiKey ?? '' }),
		continueOnFail: () => options.continueOnFailValue ?? false,
		getNode: () => FAKE_NODE,
	} as unknown as IExecuteFunctions;
}

describe('Algenta node execute()', () => {
	it('Get Contract returns the connected engine\'s discovery payload', async () => {
		handle = await startStubAlgentaServer();
		const node = new Algenta();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'get_contract', profile: 'observe' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		const [result] = await node.execute.call(ctx);
		expect(result[0].json.capabilities).toEqual(['query', 'simulate', 'recommend']);
	});

	it('Execute Decision success is parsed as a typed ExecutionReceipt', async () => {
		handle = await startStubAlgentaServer();
		const node = new Algenta();

		// Item 0 logs a decision; item 1 executes it -- two n8n items, one workflow run.
		const logCtx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'log_decision', profile: 'govern', arguments: { chosen_action: 'renew', confidence: 0.9 } }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});
		const [logged] = await node.execute.call(logCtx);
		const decisionId = logged[0].json.decision_id as string;
		expect(decisionId).toMatch(/^decision-/);

		const execCtx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'execute', decisionId, webhookUrl: 'https://example.test/hook' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});
		const [executed] = await node.execute.call(execCtx);
		expect(executed[0].json.decision_id).toBe(decisionId);
		expect(executed[0].json.execution_status).toBe('delivered');
	});

	it('a blocked Execute Decision throws a NodeApiError naming the real gate', async () => {
		handle = await startStubAlgentaServer();
		const node = new Algenta();

		const logCtx = buildContext({
			items: [{ json: {} }],
			params: [
				{ operation: 'log_decision', profile: 'govern', arguments: { chosen_action: 'renew', confidence: 0.1 } },
			],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});
		const [logged] = await node.execute.call(logCtx);
		const decisionId = logged[0].json.decision_id as string;

		const execCtx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'execute', decisionId, webhookUrl: 'https://example.test/hook' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(node.execute.call(execCtx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeApiError);
			expect((error as NodeApiError).message).toContain('confidence');
			return true;
		});
	});

	it('refuses Execute Decision under the observe profile WITHOUT calling the tool', async () => {
		handle = await startStubAlgentaServer();
		const node = new Algenta();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'observe', decisionId: 'decision-doesnt-matter' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(node.execute.call(ctx)).rejects.toBeInstanceOf(NodeOperationError);
		// The refusal happens before any network call -- the stub never saw an execute_decision.
		expect(handle.executeDecisionCalls).toHaveLength(0);
	});

	it('continueOnFail turns a blocked execution into an item-level error instead of throwing', async () => {
		handle = await startStubAlgentaServer();
		const node = new Algenta();

		const logCtx = buildContext({
			items: [{ json: {} }],
			params: [
				{ operation: 'log_decision', profile: 'govern', arguments: { chosen_action: 'renew', confidence: 0.1 } },
			],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});
		const [logged] = await node.execute.call(logCtx);
		const decisionId = logged[0].json.decision_id as string;

		const execCtx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'execute', decisionId, webhookUrl: 'https://example.test/hook' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
			continueOnFailValue: true,
		});

		const [result] = await node.execute.call(execCtx);
		expect(result[0].json.gate).toBe('confidence');
	});
});
