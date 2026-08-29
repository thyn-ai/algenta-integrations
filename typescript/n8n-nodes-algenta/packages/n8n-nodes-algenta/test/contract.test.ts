import { describe, expect, it } from 'vitest';

import {
	EXECUTE_DECISION,
	GET_CONTRACT,
	LOG_DECISION,
	PLAN_DECISION,
	QUERY_DATA,
	RECOMMEND,
	SIMULATE,
	isToolAllowedForProfile,
	isToolProfile,
} from '../src/contract';

const ALL_TOOLS = new Set([GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION, EXECUTE_DECISION]);

describe('isToolProfile', () => {
	it('accepts the four real profiles', () => {
		expect(isToolProfile('observe')).toBe(true);
		expect(isToolProfile('govern')).toBe(true);
		expect(isToolProfile('execute')).toBe(true);
		expect(isToolProfile('full')).toBe(true);
	});

	it('rejects anything else', () => {
		expect(isToolProfile('admin')).toBe(false);
		expect(isToolProfile('')).toBe(false);
	});
});

describe('isToolAllowedForProfile', () => {
	it('observe allows only the four read-only tools', () => {
		expect(isToolAllowedForProfile(GET_CONTRACT, 'observe', ALL_TOOLS)).toBe(true);
		expect(isToolAllowedForProfile(QUERY_DATA, 'observe', ALL_TOOLS)).toBe(true);
		expect(isToolAllowedForProfile(SIMULATE, 'observe', ALL_TOOLS)).toBe(true);
		expect(isToolAllowedForProfile(RECOMMEND, 'observe', ALL_TOOLS)).toBe(true);
		expect(isToolAllowedForProfile(PLAN_DECISION, 'observe', ALL_TOOLS)).toBe(false);
		expect(isToolAllowedForProfile(LOG_DECISION, 'observe', ALL_TOOLS)).toBe(false);
		expect(isToolAllowedForProfile(EXECUTE_DECISION, 'observe', ALL_TOOLS)).toBe(false);
	});

	it('govern adds plan/log but not execute', () => {
		expect(isToolAllowedForProfile(PLAN_DECISION, 'govern', ALL_TOOLS)).toBe(true);
		expect(isToolAllowedForProfile(LOG_DECISION, 'govern', ALL_TOOLS)).toBe(true);
		expect(isToolAllowedForProfile(EXECUTE_DECISION, 'govern', ALL_TOOLS)).toBe(false);
	});

	it('execute is the only profile that allows execute_decision (besides full)', () => {
		expect(isToolAllowedForProfile(EXECUTE_DECISION, 'execute', ALL_TOOLS)).toBe(true);
	});

	it('full allows anything the connected engine actually advertises', () => {
		expect(isToolAllowedForProfile('admin_only_diagnostic_tool', 'full', new Set(['admin_only_diagnostic_tool']))).toBe(
			true,
		);
		// But not something the engine does NOT advertise, even under full.
		expect(isToolAllowedForProfile('nonexistent_tool', 'full', ALL_TOOLS)).toBe(false);
	});

	it('refuses a tool the profile allows in principle but the engine does not currently advertise', () => {
		expect(isToolAllowedForProfile(EXECUTE_DECISION, 'execute', new Set([GET_CONTRACT]))).toBe(false);
	});
});
