/**
 * Asserts `../src/contract.ts`'s embedded constants agree with the real
 * `contracts/integration-tool-contract.json` file in this monorepo. Skipped (not failed) when
 * that file isn't found -- e.g. running against a published tarball outside a checkout of
 * `algenta-integrations`. Mirrors `algenta-tools/src/contract-parity.test.ts`, adapted for this
 * package's CommonJS build (`__dirname`, not `import.meta.url`).
 */
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { FULL_PROFILE_SENTINEL, NEVER_MODEL_FACING_FIELDS, TOOL_PROFILES } from '../src/contract';

interface Contract {
	profiles: {
		observe: { tools: string[] };
		govern: { extends: string; adds_tools: string[] };
		execute: { extends: string; adds_tools: string[]; never_model_facing_fields: string[] };
		full: { tools: string; opt_in_only: boolean };
	};
	mcp_tool_reference: Record<string, unknown>;
}

function findContractFile(): string | null {
	let dir = __dirname;
	for (let depth = 0; depth < 10; depth++) {
		const candidate = join(dir, 'contracts', 'integration-tool-contract.json');
		if (existsSync(candidate)) {
			return candidate;
		}
		const parent = resolve(dir, '..');
		if (parent === dir) {
			break;
		}
		dir = parent;
	}
	return null;
}

const contractPath = findContractFile();

describe.runIf(contractPath !== null)('contract parity', () => {
	const contract: Contract = JSON.parse(readFileSync(contractPath as string, 'utf-8'));

	it('observe profile matches the contract exactly', () => {
		expect(TOOL_PROFILES.observe).toEqual(new Set(contract.profiles.observe.tools));
	});

	it('govern profile matches the contract exactly', () => {
		expect(contract.profiles.govern.extends).toBe('observe');
		const expected = new Set([...contract.profiles.observe.tools, ...contract.profiles.govern.adds_tools]);
		expect(TOOL_PROFILES.govern).toEqual(expected);
	});

	it('execute profile matches the contract exactly', () => {
		expect(contract.profiles.execute.extends).toBe('govern');
		const governTools = TOOL_PROFILES.govern as ReadonlySet<string>;
		const expected = new Set([...governTools, ...contract.profiles.execute.adds_tools]);
		expect(TOOL_PROFILES.execute).toEqual(expected);
	});

	it('full profile is the wildcard sentinel', () => {
		expect(contract.profiles.full.tools).toBe('*');
		expect(contract.profiles.full.opt_in_only).toBe(true);
		expect(TOOL_PROFILES.full).toBe(FULL_PROFILE_SENTINEL);
	});

	it('never-model-facing fields match the contract', () => {
		expect(NEVER_MODEL_FACING_FIELDS).toEqual(new Set(contract.profiles.execute.never_model_facing_fields));
	});

	it('every mcp_tool_reference name is used by some profile', () => {
		const referencedNames = new Set(Object.keys(contract.mcp_tool_reference));
		const profiledNames = TOOL_PROFILES.execute as ReadonlySet<string>;
		expect(referencedNames).toEqual(new Set(profiledNames));
	});
});
