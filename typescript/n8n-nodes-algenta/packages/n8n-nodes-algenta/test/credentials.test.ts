import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

import type { INodeProperties } from 'n8n-workflow';
import { describe, expect, it } from 'vitest';

import { AlgentaApi } from '../credentials/AlgentaApi.credentials';
import * as entryPoint from '../index';
import { Algenta } from '../nodes/Algenta/Algenta.node';
import packageJson from '../package.json';

const PACKAGE_ROOT = resolve(__dirname, '..');
const CREDENTIAL_SOURCE = resolve(PACKAGE_ROOT, 'credentials/AlgentaApi.credentials.ts');
const NODE_SOURCE = resolve(PACKAGE_ROOT, 'nodes/Algenta/Algenta.node.ts');

/** Resolves an n8n `file:` icon reference the way n8n does: relative to the file declaring it. */
function iconPath(declaringFile: string, reference: string): string {
	expect(reference.startsWith('file:')).toBe(true);
	return resolve(dirname(declaringFile), reference.slice('file:'.length));
}

function property(credential: AlgentaApi, name: string): INodeProperties {
	const found = credential.properties.find((candidate) => candidate.name === name);
	if (found === undefined) {
		throw new Error(`credential declares no '${name}' property`);
	}
	return found;
}

describe('AlgentaApi credential', () => {
	const credential = new AlgentaApi();
	const node = new Algenta();

	it('is exactly the credential type the Algenta node requires', () => {
		expect(credential.name).toBe('algentaApi');
		expect(node.description.credentials).toEqual([{ name: 'algentaApi', required: true }]);
		expect(credential.displayName).toBe('Algenta API');
		expect(credential.documentationUrl).toMatch(/^https:\/\//);
	});

	it("declares exactly the two fields the node's execute() reads, with the API key masked", () => {
		expect(credential.properties.map((candidate) => candidate.name)).toEqual(['baseUrl', 'apiKey']);

		const baseUrl = property(credential, 'baseUrl');
		expect(baseUrl.type).toBe('string');
		expect(baseUrl.default).toBe('http://localhost:8000');
		// The node appends /mcp itself (see src/mcp-client.ts); the field must tell the operator so.
		expect(baseUrl.description).toContain('/mcp');

		const apiKey = property(credential, 'apiKey');
		expect(apiKey.type).toBe('string');
		expect(apiKey.typeOptions).toEqual({ password: true });
		expect(apiKey.default).toBe('');
	});

	it("tests a connection against the engine's health route under the configured base URL", () => {
		expect(credential.test).toEqual({
			request: { baseURL: '={{$credentials.baseUrl}}', url: '/v1/health', method: 'GET' },
		});
	});

	it('points its light and dark icons at the same two SVGs the node ships', () => {
		const icon = credential.icon as { light: string; dark: string };
		const nodeIcon = node.description.icon as { light: string; dark: string };
		for (const variant of ['light', 'dark'] as const) {
			const resolved = iconPath(CREDENTIAL_SOURCE, icon[variant]);
			expect(existsSync(resolved)).toBe(true);
			expect(iconPath(NODE_SOURCE, nodeIcon[variant])).toBe(resolved);
		}
	});
});

describe('package entry point and n8n manifest', () => {
	it('re-exports the node and credential classes n8n loads', () => {
		expect(entryPoint.Algenta).toBe(Algenta);
		expect(entryPoint.AlgentaApi).toBe(AlgentaApi);
	});

	it("lists, in package.json's n8n manifest, the built counterparts of exactly those two sources", () => {
		const toSource = (built: string): string =>
			resolve(PACKAGE_ROOT, built.replace(/^dist\//, '').replace(/\.js$/, '.ts'));
		expect(packageJson.n8n.credentials.map(toSource)).toEqual([CREDENTIAL_SOURCE]);
		expect(packageJson.n8n.nodes.map(toSource)).toEqual([NODE_SOURCE]);
		expect(existsSync(CREDENTIAL_SOURCE)).toBe(true);
		expect(existsSync(NODE_SOURCE)).toBe(true);
	});
});
