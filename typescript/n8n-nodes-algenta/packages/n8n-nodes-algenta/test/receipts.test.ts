import { describe, expect, it } from 'vitest';

import { parseExecutionBlockedBody, parseExecutionReceipt } from '../src/receipts';

describe('parseExecutionReceipt', () => {
	it('parses a real receipt shape', () => {
		const receipt = parseExecutionReceipt({
			decision_id: 'decision-1',
			webhook_url: 'https://example.test/hook',
			execution_status: 'delivered',
			response_code: 200,
			executed_at: '2026-01-01T00:00:00Z',
			policy_snapshot_id: 'snap-1',
			schema_snapshot_id: null,
			manifest_version: 1,
			payload_summary: { delivered: true },
			safety_overridden: false,
		});
		expect(receipt).not.toBeNull();
		expect(receipt?.decision_id).toBe('decision-1');
	});

	it('passes through unknown extra fields (forward-compatible)', () => {
		const receipt = parseExecutionReceipt({
			decision_id: 'decision-1',
			webhook_url: 'https://example.test/hook',
			execution_status: 'delivered',
			executed_at: '2026-01-01T00:00:00Z',
			a_future_field_this_package_has_never_heard_of: 'value',
		});
		expect(receipt).not.toBeNull();
		expect((receipt as unknown as Record<string, unknown>).a_future_field_this_package_has_never_heard_of).toBe(
			'value',
		);
	});

	it('returns null for a non-receipt-shaped object', () => {
		expect(parseExecutionReceipt({ capabilities: ['query'] })).toBeNull();
	});

	it('returns null for a non-object', () => {
		expect(parseExecutionReceipt('a string')).toBeNull();
		expect(parseExecutionReceipt(null)).toBeNull();
		expect(parseExecutionReceipt([1, 2, 3])).toBeNull();
	});
});

describe('parseExecutionBlockedBody', () => {
	it('parses the real three-named-gate denial shape', () => {
		const blocked = parseExecutionBlockedBody({
			error: {
				code: 'execution_blocked_confidence',
				gate: 'confidence',
				message: 'confidence 0.1 is below the policy minimum of 0.5.',
				override_hint: 'Set override_safety=true to bypass the confidence gate.',
			},
		});
		expect(blocked).toEqual({
			code: 'execution_blocked_confidence',
			gate: 'confidence',
			message: 'confidence 0.1 is below the policy minimum of 0.5.',
			overrideHint: 'Set override_safety=true to bypass the confidence gate.',
		});
	});

	it('accepts a gate name it does not yet know about', () => {
		const blocked = parseExecutionBlockedBody({
			error: { code: 'execution_blocked_future_gate', gate: 'future_gate', message: 'blocked' },
		});
		expect(blocked?.gate).toBe('future_gate');
	});

	it('returns null for a generic upstream failure with no named gate', () => {
		expect(parseExecutionBlockedBody({ message: 'internal error' })).toBeNull();
	});
});
