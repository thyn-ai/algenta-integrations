import { afterEach, describe, expect, it } from 'vitest';

import { connectAlgentaMcp, McpToolError } from '../src/mcp-client';
import { startStubAlgentaServer, type StubServerHandle } from './test-support/stub-server';

let handle: StubServerHandle | undefined;

afterEach(async () => {
	if (handle) {
		await handle.close();
		handle = undefined;
	}
});

describe('connectAlgentaMcp', () => {
	it('calls a freeform tool and unwraps structuredContent over the real wire', async () => {
		handle = await startStubAlgentaServer();
		const baseUrl = handle.baseUrl.replace(/\/mcp$/, '');

		const connection = await connectAlgentaMcp(baseUrl, '');
		try {
			const result = (await connection.callTool('simulate', { scenario: 'renewal' })) as {
				scenario: string;
				expected_value: number;
			};
			expect(result.scenario).toBe('renewal');
			expect(result.expected_value).toBe(42.0);
		} finally {
			await connection.close();
		}
	});

	it('round-trips a real ExecutionReceipt through log_decision -> execute_decision', async () => {
		handle = await startStubAlgentaServer();
		const baseUrl = handle.baseUrl.replace(/\/mcp$/, '');
		const connection = await connectAlgentaMcp(baseUrl, '');

		try {
			const logged = (await connection.callTool('log_decision', {
				chosen_action: 'renew',
				confidence: 0.9,
			})) as { decision_id: string };
			expect(logged.decision_id).toMatch(/^decision-/);

			const receipt = (await connection.callTool('execute_decision', {
				decision_id: logged.decision_id,
				webhook_url: 'https://example.test/hook',
			})) as { decision_id: string; execution_status: string };

			expect(receipt.decision_id).toBe(logged.decision_id);
			expect(receipt.execution_status).toBe('delivered');
			expect(handle.executeDecisionCalls).toHaveLength(1);
			expect(handle.executeDecisionCalls[0].decision_id).toBe(logged.decision_id);
		} finally {
			await connection.close();
		}
	});

	it('throws McpToolError with the real named-gate denial body for a blocked execute_decision', async () => {
		handle = await startStubAlgentaServer();
		const baseUrl = handle.baseUrl.replace(/\/mcp$/, '');
		const connection = await connectAlgentaMcp(baseUrl, '');

		try {
			const logged = (await connection.callTool('log_decision', {
				chosen_action: 'renew',
				confidence: 0.1, // below POLICY_MIN_CONFIDENCE (0.5) in the stub server
			})) as { decision_id: string };

			await expect(
				connection.callTool('execute_decision', {
					decision_id: logged.decision_id,
					webhook_url: 'https://example.test/hook',
				}),
			).rejects.toSatisfy((error: unknown) => {
				expect(error).toBeInstanceOf(McpToolError);
				const detail = JSON.parse((error as McpToolError).detail);
				expect(detail.error.gate).toBe('confidence');
				return true;
			});
		} finally {
			await connection.close();
		}
	});
});
