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

describe('Algenta node execute(): the Arguments parameter', () => {
	it('parses a JSON string (how n8n delivers a json-type parameter) into the tool call', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'simulate', profile: 'observe', arguments: '{"scenario": "renewal"}' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		const [result] = await new Algenta().execute.call(ctx);
		expect(result[0].json).toEqual({ scenario: 'renewal', expected_value: 42 });
		expect(result[0].pairedItem).toBe(0);
	});

	it('rejects malformed JSON with a NodeOperationError that says so, before any tool call', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'simulate', profile: 'observe', arguments: '{"scenario": ' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			expect((error as NodeOperationError).description).toBe('Arguments must be valid JSON');
			return true;
		});
	});

	it('rejects valid JSON that is not an object: a tool call takes named arguments', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'simulate', profile: 'observe', arguments: '["renewal"]' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			expect((error as NodeOperationError).message).toBe('Arguments must be a JSON object');
			return true;
		});
	});

	it.each([
		['an empty string', ''],
		['whitespace only', '   '],
		['a number', 42],
		['a boolean', true],
		['null', null],
	])('sends an empty object when Arguments is %s, so the engine reports what is missing', async (_label, raw) => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'simulate', profile: 'observe', arguments: raw }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			// The stub's own input validation names the field an empty object lacks -- proof that
			// `{}` (and not the raw value) is what reached the engine.
			expect((error as NodeOperationError).message).toContain('scenario');
			return true;
		});
	});
});

describe('Algenta node execute(): error handling', () => {
	it('continueOnFail turns a profile refusal into an item-level error without calling the tool', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'observe', decisionId: 'decision-1', webhookUrl: 'https://example.test/hook' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
			continueOnFailValue: true,
		});

		const [result] = await new Algenta().execute.call(ctx);
		expect(result).toHaveLength(1);
		expect(result[0].json.error).toContain("not allowed under the 'observe' tool profile");
		expect(result[0].pairedItem).toBe(0);
		expect(handle.executeDecisionCalls).toHaveLength(0);
	});

	it('an unrecognised profile value falls back to the observe default', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'superuser', decisionId: 'decision-1', webhookUrl: 'https://example.test/hook' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			expect((error as NodeOperationError).message).toContain("under the 'observe' tool profile");
			return true;
		});
		expect(handle.executeDecisionCalls).toHaveLength(0);
	});

	it("wraps a tool error that is not a named-gate denial as a NodeOperationError carrying the engine's message", async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'query_data', profile: 'observe', arguments: {} }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			expect(error).not.toBeInstanceOf(NodeApiError);
			expect((error as NodeOperationError).message).toContain('dataset');
			return true;
		});
	});

	it('does not mistake an Execute Decision error without a gate body for a policy denial', async () => {
		handle = await startStubAlgentaServer();
		// A number where the engine wants a string -- what an n8n expression can hand the node. The
		// stub's input validation rejects it with plain text, not the `{"error": {gate...}}` body.
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'execute_decision', profile: 'execute', decisionId: 'decision-1', webhookUrl: 12345 }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			expect(error).not.toBeInstanceOf(NodeApiError);
			expect((error as NodeOperationError).message).toContain('webhook_url');
			return true;
		});
		// Validation stopped the call before the tool handler ran.
		expect(handle.executeDecisionCalls).toHaveLength(0);
	});

	it('processes every input item in order, pairing each output with its own item', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }, { json: {} }],
			params: [
				{ operation: 'get_contract', profile: 'observe' },
				{ operation: 'simulate', profile: 'observe', arguments: '{"scenario": "second"}' },
			],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		const [result] = await new Algenta().execute.call(ctx);
		expect(result).toHaveLength(2);
		expect(result[0].pairedItem).toBe(0);
		expect(result[0].json.engine_version).toBe('1.4.0');
		expect(result[1].pairedItem).toBe(1);
		expect(result[1].json.scenario).toBe('second');
	});
});

describe('Algenta node execute(): the full profile', () => {
	it('passes an off-contract tool the engine advertises straight through, with empty arguments', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'admin_only_diagnostic_tool', profile: 'full' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		const [result] = await new Algenta().execute.call(ctx);
		expect(result[0].json).toEqual({ ok: true });
	});

	it('still refuses that same off-contract tool under the widest fixed profile', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }],
			params: [{ operation: 'admin_only_diagnostic_tool', profile: 'execute' }],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		await expect(new Algenta().execute.call(ctx)).rejects.toSatisfy((error: unknown) => {
			expect(error).toBeInstanceOf(NodeOperationError);
			expect((error as NodeOperationError).message).toContain("under the 'execute' tool profile");
			return true;
		});
	});

	it('wraps a tool result that is not a JSON object as { result }', async () => {
		handle = await startStubAlgentaServer();
		const ctx = buildContext({
			items: [{ json: {} }, { json: {} }],
			params: [
				{ operation: 'admin_only_plain_text_tool', profile: 'full' },
				{ operation: 'admin_only_empty_result_tool', profile: 'full' },
			],
			baseUrl: handle.baseUrl.replace(/\/mcp$/, ''),
		});

		const [result] = await new Algenta().execute.call(ctx);
		expect(result[0].json).toEqual({ result: 'pong' });
		expect(result[1].json).toEqual({ result: null });
	});
});
