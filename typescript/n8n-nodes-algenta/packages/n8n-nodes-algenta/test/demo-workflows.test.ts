import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import type { IExecuteFunctions, INode, INodeExecutionData } from 'n8n-workflow';
import { afterEach, describe, expect, it } from 'vitest';

import { Algenta } from '../nodes/Algenta/Algenta.node';
import { OBSERVE_TOOLS } from '../src/contract';
import { startStubAlgentaServer, type StubServerHandle } from './test-support/stub-server';

const DEMO_DIR = resolve(__dirname, '../../../../../demo/n8n');

interface DemoWorkflow {
	name: string;
	nodes: Array<{
		id: string;
		name: string;
		type: string;
		parameters?: Record<string, unknown>;
	}>;
	connections: Record<string, { main: Array<Array<{ node: string; type: string; index: number }>> }>;
	meta?: { instructions?: string[] };
}

function loadWorkflow(fileName: string): DemoWorkflow {
	const path = resolve(DEMO_DIR, fileName);
	return JSON.parse(readFileSync(path, 'utf8')) as DemoWorkflow;
}

const FAKE_NODE: INode = {
	id: '1',
	name: 'Algenta',
	type: 'n8n-nodes-algenta.algenta',
	typeVersion: 1,
	position: [0, 0],
	parameters: {},
};

function buildContext(options: {
	items: INodeExecutionData[];
	params: Array<Record<string, unknown>>;
	baseUrl: string;
	apiKey?: string;
}): IExecuteFunctions {
	return {
		getInputData: () => options.items,
		getNodeParameter: (name: string, itemIndex: number, fallback?: unknown) => {
			const value = options.params[itemIndex]?.[name];
			return value === undefined ? fallback : value;
		},
		getCredentials: async () => ({ baseUrl: options.baseUrl, apiKey: options.apiKey ?? '' }),
		continueOnFail: () => false,
		getNode: () => FAKE_NODE,
	} as unknown as IExecuteFunctions;
}

let handle: StubServerHandle | undefined;

afterEach(async () => {
	if (handle) {
		await handle.close();
		handle = undefined;
	}
});

describe('demo/n8n/observe-weekly-report.json', () => {
	it('is valid JSON with the expected workflow shape', () => {
		const workflow = loadWorkflow('observe-weekly-report.json');
		expect(workflow.name).toBe('Algenta: observe-profile weekly report');
		expect(workflow.nodes).toBeInstanceOf(Array);
		expect(workflow.nodes.length).toBeGreaterThan(0);
		expect(workflow.connections).toBeInstanceOf(Object);
		expect(workflow.meta?.instructions).toBeInstanceOf(Array);
	});

	it('uses only observe-profile Algenta operations', () => {
		const workflow = loadWorkflow('observe-weekly-report.json');
		const algentaNodes = workflow.nodes.filter((node) => node.type === 'n8n-nodes-algenta.algenta');
		expect(algentaNodes.length).toBeGreaterThan(0);

		for (const node of algentaNodes) {
			const operation = node.parameters?.operation as string;
			const profile = node.parameters?.profile as string;
			expect(profile).toBe('observe');
			expect(OBSERVE_TOOLS.has(operation)).toBe(true);
		}
	});

	it('has connections that wire every node into a single linear chain', () => {
		const workflow = loadWorkflow('observe-weekly-report.json');
		const nodeNames = new Set(workflow.nodes.map((node) => node.name));

		for (const [sourceName, outputs] of Object.entries(workflow.connections)) {
			expect(nodeNames.has(sourceName)).toBe(true);
			expect(outputs.main).toBeInstanceOf(Array);
			for (const branch of outputs.main) {
				for (const target of branch) {
					expect(nodeNames.has(target.node)).toBe(true);
					expect(target.type).toBe('main');
				}
			}
		}
	});

	it('runs every observe tool against the stub server with deterministic outputs', async () => {
		handle = await startStubAlgentaServer();
		const baseUrl = handle.baseUrl.replace(/\/mcp$/, '');
		const node = new Algenta();

		const [contract] = await node.execute.call(
			buildContext({
				items: [{ json: {} }],
				params: [{ operation: 'get_contract', profile: 'observe' }],
				baseUrl,
			}),
		);
		expect(contract[0].json.engine_version).toBe('1.4.0');
		expect(contract[0].json.capabilities).toEqual(['query', 'simulate', 'recommend']);

		const [query] = await node.execute.call(
			buildContext({
				items: [{ json: {} }],
				params: [
					{
						operation: 'query_data',
						profile: 'observe',
						arguments: { dataset: 'weekly_renewals' },
					},
				],
				baseUrl,
			}),
		);
		expect(query[0].json.dataset).toBe('weekly_renewals');
		expect(query[0].json.rows).toEqual([{ value: 1 }, { value: 2 }]);

		const [simulate] = await node.execute.call(
			buildContext({
				items: [{ json: {} }],
				params: [
					{
						operation: 'simulate',
						profile: 'observe',
						arguments: { scenario: 'next_week_renewal_forecast' },
					},
				],
				baseUrl,
			}),
		);
		expect(simulate[0].json.scenario).toBe('next_week_renewal_forecast');
		expect(simulate[0].json.expected_value).toBe(42);

		const [recommend] = await node.execute.call(
			buildContext({
				items: [{ json: {} }],
				params: [
					{
						operation: 'recommend',
						profile: 'observe',
						arguments: { scenario: 'next_week_renewal_recommendation' },
					},
				],
				baseUrl,
			}),
		);
		expect(recommend[0].json.scenario).toBe('next_week_renewal_recommendation');
		expect(recommend[0].json.recommended_action).toBe('hold');
		expect(recommend[0].json.confidence).toBe(0.87);
	});

	it('render node references only nodes that exist in the workflow', () => {
		const workflow = loadWorkflow('observe-weekly-report.json');
		const nodeNames = new Set(workflow.nodes.map((node) => node.name));
		const renderNode = workflow.nodes.find((node) => node.name === 'Render Weekly Report');
		expect(renderNode).toBeDefined();

		const value = (renderNode?.parameters?.assignments as { assignments: Array<{ value: string }> })?.assignments?.[0]
			?.value;
		expect(typeof value).toBe('string');

		const referencedNodes = Array.from(value.matchAll(/\$\('([^']+)'\)/g)).map((match) => match[1]);
		expect(referencedNodes.length).toBeGreaterThan(0);
		for (const referencedName of referencedNodes) {
			expect(nodeNames.has(referencedName)).toBe(true);
		}
	});
});
