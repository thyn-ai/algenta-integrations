/**
 * `execute_decision` is the only Algenta MCP tool with a typed success/denial contract -- see
 * `algenta-tools/src/receipts.ts` (the sibling TypeScript package in this monorepo) for the full
 * rationale; this is the same schema, duplicated rather than imported for the same reason
 * `algenta-tools/src/contract.ts` documents.
 *
 * Hand-written validation, not zod: `n8n-workflow` itself declares zod as a peer dependency
 * pinned to its own v3, and this package has no other need for a schema library strong enough to
 * justify a second, conflicting major version living alongside it in a community node's install
 * footprint. The shape here is small and stable enough that a few `typeof`/`Array.isArray` checks
 * are the more honest dependency-free tool for the job.
 *
 * - Success (200) returns an ExecutionReceipt.
 * - A blocked call returns a synchronous denial (surfaced over MCP as a tool-error result) naming
 *   exactly one of three real, literal safety gates: "idempotency", "confidence", or
 *   "risk_floor".
 *
 * There is no third, "pending" outcome -- a call either succeeds or is blocked, synchronously, in
 * the same call.
 */

export const EXECUTION_GATES = ['idempotency', 'confidence', 'risk_floor'] as const;
export type ExecutionGate = (typeof EXECUTION_GATES)[number];

/** Extra fields the engine may add over time pass through unchanged -- this type only names the
 * fields this package actually reads. */
export interface ExecutionReceipt {
	decision_id: string;
	webhook_url: string;
	execution_status: 'delivered' | 'failed';
	response_code: number | null;
	executed_at: string;
	policy_snapshot_id: string | null;
	schema_snapshot_id: string | null;
	manifest_version: number | string | null;
	payload_summary: unknown;
	safety_overridden: boolean;
	[key: string]: unknown;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNullOr<T>(value: unknown, check: (v: unknown) => v is T): value is T | null {
	return value === null || check(value);
}

const isString = (v: unknown): v is string => typeof v === 'string';
const isNumber = (v: unknown): v is number => typeof v === 'number';

/** Returns `null` (rather than throwing) when `raw` doesn't validate as a real `execute_decision`
 * success receipt. */
export function parseExecutionReceipt(raw: unknown): ExecutionReceipt | null {
	if (!isPlainObject(raw)) {
		return null;
	}
	if (!isString(raw.decision_id) || !isString(raw.webhook_url) || !isString(raw.executed_at)) {
		return null;
	}
	if (raw.execution_status !== 'delivered' && raw.execution_status !== 'failed') {
		return null;
	}
	if (!isNullOr(raw.response_code ?? null, isNumber)) {
		return null;
	}
	if (!isNullOr(raw.policy_snapshot_id ?? null, isString)) {
		return null;
	}
	if (!isNullOr(raw.schema_snapshot_id ?? null, isString)) {
		return null;
	}
	const manifestVersion = raw.manifest_version ?? null;
	if (manifestVersion !== null && !isNumber(manifestVersion) && !isString(manifestVersion)) {
		return null;
	}

	return {
		...raw,
		decision_id: raw.decision_id,
		webhook_url: raw.webhook_url,
		execution_status: raw.execution_status,
		response_code: (raw.response_code as number | null) ?? null,
		executed_at: raw.executed_at,
		policy_snapshot_id: (raw.policy_snapshot_id as string | null) ?? null,
		schema_snapshot_id: (raw.schema_snapshot_id as string | null) ?? null,
		manifest_version: manifestVersion as number | string | null,
		payload_summary: raw.payload_summary ?? null,
		safety_overridden: Boolean(raw.safety_overridden),
	};
}

export interface ExecutionBlockedBody {
	code: string;
	gate: string;
	message: string;
	overrideHint: string | null;
}

/** Parses an MCP tool-error result's payload as the real execute_decision denial body
 * (`{"error": {"code", "gate", "message", "override_hint"}}`). Returns `null` for anything else,
 * so the caller can fall back to a plain tool-failure error instead of inventing a gate that
 * wasn't named. `gate` is typed as `string`, not a literal union of {@link EXECUTION_GATES} --
 * a future fourth gate this package doesn't know about yet should still surface with its real
 * name, not fail to parse. */
export function parseExecutionBlockedBody(raw: unknown): ExecutionBlockedBody | null {
	if (!isPlainObject(raw) || !isPlainObject(raw.error)) {
		return null;
	}
	const { error } = raw;
	if (!isString(error.code) || !isString(error.gate) || !isString(error.message)) {
		return null;
	}
	const overrideHint = error.override_hint ?? null;
	if (overrideHint !== null && !isString(overrideHint)) {
		return null;
	}
	return { code: error.code, gate: error.gate, message: error.message, overrideHint: overrideHint as string | null };
}
