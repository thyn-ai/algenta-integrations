/**
 * Property-based tests (fast-check) for this package's HAND-WRITTEN `execute_decision` validators
 * (`src/receipts.ts` deliberately avoids a schema library -- see its header). A hand-rolled
 * validator is exactly where a single missed `typeof` slips through unnoticed, so these check what
 * must hold for EVERY payload rather than the handful `receipts.test.ts` spells out: totality
 * (never throw), round-trip fidelity, defaults, forward-compatible passthrough, and that
 * corrupting any single validated field is enough to reject.
 */
import * as fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { EXECUTION_GATES, parseExecutionBlockedBody, parseExecutionReceipt } from '../src/receipts';

// ── Fixtures ──────────────────────────────────────────────────────────────────────────────────

/** The four fields with no default: dropping any one of them must reject the receipt. */
const REQUIRED_RECEIPT_KEYS = ['decision_id', 'webhook_url', 'executed_at', 'execution_status'] as const;

/** Every field the parser emits, read off the parser itself (there is no schema object to
 * introspect), so a newly defaulted field automatically joins the key check below. */
const RECEIPT_KEYS: readonly string[] = Object.keys(
	parseExecutionReceipt({
		decision_id: 'd',
		webhook_url: 'https://example.test/hook',
		executed_at: '2026-01-01T00:00:00Z',
		execution_status: 'delivered',
	}) ?? {},
);

const DENIAL_KEYS: readonly string[] = ['code', 'gate', 'message', 'override_hint'];

// ── Arbitraries ───────────────────────────────────────────────────────────────────────────────

/** snake_case identifiers. `__proto__` is excluded on purpose: assigning it as a plain-object key
 * sets the prototype instead of a property, a JavaScript hazard unrelated to anything here. */
const identArb = fc
	.string({ unit: fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789_'), minLength: 1, maxLength: 20 })
	.filter((name) => name !== '__proto__');

/** JSON values with integer-only numbers and identifier keys. */
const jsonArb: fc.Arbitrary<unknown> = fc.letrec((tie) => ({
	json: fc.oneof(
		{ depthSize: 'small' },
		fc.constant(null),
		fc.boolean(),
		fc.integer(),
		fc.string(),
		fc.array(tie('json'), { maxLength: 4 }),
		fc.dictionary(identArb, tie('json'), { maxKeys: 4, noNullPrototype: true }),
	),
})).json;

const nullable = <T>(arb: fc.Arbitrary<T>): fc.Arbitrary<T | null> => fc.option(arb, { nil: null });

/** Values that are definitely not a string -- including `undefined`, i.e. "field missing". */
const nonStringArb: fc.Arbitrary<unknown> = fc.oneof(
	fc.constant(undefined),
	fc.constant(null),
	fc.integer(),
	fc.boolean(),
	fc.array(fc.string(), { maxLength: 2 }),
	fc.dictionary(identArb, fc.string(), { maxKeys: 2, noNullPrototype: true }),
);

/** Values that are neither an object nor an array: the parsers must reject all of them. */
const nonObjectArb: fc.Arbitrary<unknown> = fc.oneof(
	fc.string(),
	fc.double(),
	fc.boolean(),
	fc.constant(null),
	fc.constant(undefined),
	fc.bigInt(),
	fc.array(fc.anything(), { maxLength: 3 }),
);

/** Fields the engine may add in a future version: they must pass through unchanged. */
const extrasArb = fc.dictionary(
	identArb.filter((key) => !RECEIPT_KEYS.includes(key)),
	jsonArb,
	{ maxKeys: 4, noNullPrototype: true },
);

interface RawReceipt {
	decision_id: string;
	webhook_url: string;
	executed_at: string;
	execution_status: 'delivered' | 'failed';
	response_code?: number | null;
	policy_snapshot_id?: string | null;
	schema_snapshot_id?: string | null;
	manifest_version?: number | string | null;
	payload_summary?: unknown;
	safety_overridden?: unknown;
}

/** A valid receipt: the four required fields always present, every defaulted field independently
 * present-or-absent, so the parser's defaults are exercised as often as its explicit values.
 * `safety_overridden` is ANY JSON value: this validator coerces it with `Boolean()` rather than
 * rejecting a non-boolean, and the round-trip property below pins that down. */
const receiptArb: fc.Arbitrary<RawReceipt> = fc.record(
	{
		decision_id: fc.string(),
		webhook_url: fc.string(),
		executed_at: fc.string(),
		execution_status: fc.constantFrom('delivered', 'failed'),
		response_code: nullable(fc.integer()),
		policy_snapshot_id: nullable(fc.string()),
		schema_snapshot_id: nullable(fc.string()),
		manifest_version: nullable(fc.oneof(fc.integer(), fc.string())),
		payload_summary: jsonArb,
		safety_overridden: jsonArb,
	},
	{ requiredKeys: [...REQUIRED_RECEIPT_KEYS], noNullPrototype: true },
);

/** A valid receipt plus forward-compatible extras, as the engine would actually send it. */
const receiptWithExtrasArb = fc.tuple(receiptArb, extrasArb).map(([receipt, extras]) => ({
	raw: { ...extras, ...receipt } as Record<string, unknown>,
	receipt,
	extras,
}));

interface RawDenial {
	code: string;
	gate: string;
	message: string;
	override_hint?: string | null;
}

/** The real denial body. `gate` is drawn from the three known gates AND from arbitrary names: a
 * fourth gate this package has never heard of must surface with its real name, not fail to parse. */
const denialArb: fc.Arbitrary<RawDenial> = fc.record(
	{
		code: fc.string(),
		gate: fc.oneof(fc.constantFrom(...EXECUTION_GATES), identArb),
		message: fc.string(),
		override_hint: nullable(fc.string()),
	},
	{ requiredKeys: ['code', 'gate', 'message'], noNullPrototype: true },
);

/** `{"error": {...}}` as it arrives over MCP, with unknown extra keys at both levels. */
const denialEnvelopeArb = fc
	.tuple(
		denialArb,
		fc.dictionary(identArb.filter((key) => !DENIAL_KEYS.includes(key)), jsonArb, { maxKeys: 3, noNullPrototype: true }),
		fc.dictionary(identArb.filter((key) => key !== 'error'), jsonArb, { maxKeys: 2, noNullPrototype: true }),
	)
	.map(([error, errorExtras, topLevelExtras]) => ({
		envelope: { ...topLevelExtras, error: { ...errorExtras, ...error } } as Record<string, unknown>,
		error,
	}));

// ── parseExecutionReceipt ─────────────────────────────────────────────────────────────────────

describe('parseExecutionReceipt (properties)', () => {
	it('is total: never throws, and yields null or a plain object for anything at all', () => {
		fc.assert(
			fc.property(fc.anything(), (raw) => {
				const parsed = parseExecutionReceipt(raw);
				expect(parsed === null || (typeof parsed === 'object' && !Array.isArray(parsed))).toBe(true);
			}),
		);
	});

	it('rejects every non-object payload', () => {
		fc.assert(
			fc.property(nonObjectArb, (raw) => {
				expect(parseExecutionReceipt(raw)).toBeNull();
			}),
		);
	});

	it('round-trips a valid receipt: required fields verbatim, absent fields defaulted, extras passed through', () => {
		fc.assert(
			fc.property(receiptWithExtrasArb, ({ raw, receipt, extras }) => {
				const parsed = parseExecutionReceipt(raw);
				expect(parsed).not.toBeNull();
				const out = parsed!;

				expect(out.decision_id).toBe(receipt.decision_id);
				expect(out.webhook_url).toBe(receipt.webhook_url);
				expect(out.executed_at).toBe(receipt.executed_at);
				expect(out.execution_status).toBe(receipt.execution_status);
				expect(out.response_code).toBe(receipt.response_code ?? null);
				expect(out.policy_snapshot_id).toBe(receipt.policy_snapshot_id ?? null);
				expect(out.schema_snapshot_id).toBe(receipt.schema_snapshot_id ?? null);
				expect(out.manifest_version).toBe(receipt.manifest_version ?? null);
				expect(out.payload_summary).toEqual(receipt.payload_summary ?? null);
				// Coerced, never rejected -- absent reads as false, any other value as its truthiness.
				expect(out.safety_overridden).toBe(Boolean(receipt.safety_overridden));

				for (const [key, value] of Object.entries(extras)) {
					expect(out[key]).toEqual(value);
				}
				// The parser adds exactly its defaulted fields and invents nothing else.
				expect(Object.keys(out).sort()).toEqual([...new Set([...Object.keys(raw), ...RECEIPT_KEYS])].sort());
			}),
		);
	});

	it('is a fixed point: a parsed receipt re-parses to itself', () => {
		fc.assert(
			fc.property(receiptWithExtrasArb, ({ raw }) => {
				const once = parseExecutionReceipt(raw);
				expect(once).not.toBeNull();
				expect(parseExecutionReceipt(once)).toEqual(once);
			}),
		);
	});

	it('rejects a receipt missing any one of its required fields', () => {
		fc.assert(
			fc.property(receiptWithExtrasArb, fc.constantFrom(...REQUIRED_RECEIPT_KEYS), ({ raw }, field) => {
				const incomplete = { ...raw };
				delete incomplete[field];
				expect(parseExecutionReceipt(incomplete)).toBeNull();
			}),
		);
	});

	it('rejects a receipt with any one validated field of the wrong type', () => {
		const corruptions: ReadonlyArray<readonly [keyof RawReceipt, fc.Arbitrary<unknown>]> = [
			['decision_id', nonStringArb],
			['webhook_url', nonStringArb],
			['executed_at', nonStringArb],
			['execution_status', fc.oneof(fc.string().filter((s) => s !== 'delivered' && s !== 'failed'), nonStringArb)],
			['response_code', fc.oneof(fc.string(), fc.boolean(), fc.constant({}), fc.array(fc.integer(), { maxLength: 2 }))],
			['policy_snapshot_id', fc.oneof(fc.integer(), fc.boolean(), fc.constant({}))],
			['schema_snapshot_id', fc.oneof(fc.integer(), fc.boolean(), fc.constant({}))],
			['manifest_version', fc.oneof(fc.boolean(), fc.constant({}), fc.array(fc.integer(), { maxLength: 2 }))],
		];
		const corruptionArb = fc
			.constantFrom(...corruptions)
			.chain(([field, badValueArb]) => fc.tuple(fc.constant(field), badValueArb));

		fc.assert(
			fc.property(receiptWithExtrasArb, corruptionArb, ({ raw }, [field, bad]) => {
				expect(parseExecutionReceipt({ ...raw, [field]: bad })).toBeNull();
			}),
		);
	});

	it('never mutates its input', () => {
		fc.assert(
			fc.property(receiptWithExtrasArb, ({ raw }) => {
				const before = structuredClone(raw);
				parseExecutionReceipt(raw);
				expect(raw).toEqual(before);
			}),
		);
	});
});

// ── parseExecutionBlockedBody ─────────────────────────────────────────────────────────────────

describe('parseExecutionBlockedBody (properties)', () => {
	it('is total, and only ever yields the four-field denial or null', () => {
		fc.assert(
			fc.property(fc.anything(), (raw) => {
				const parsed = parseExecutionBlockedBody(raw);
				if (parsed !== null) {
					expect(Object.keys(parsed).sort()).toEqual(['code', 'gate', 'message', 'overrideHint']);
					expect(typeof parsed.code).toBe('string');
					expect(typeof parsed.gate).toBe('string');
					expect(typeof parsed.message).toBe('string');
					expect(parsed.overrideHint === null || typeof parsed.overrideHint === 'string').toBe(true);
				}
			}),
		);
	});

	it('maps a real denial envelope exactly, carrying any gate name verbatim', () => {
		fc.assert(
			fc.property(denialEnvelopeArb, ({ envelope, error }) => {
				expect(parseExecutionBlockedBody(envelope)).toEqual({
					code: error.code,
					gate: error.gate,
					message: error.message,
					overrideHint: error.override_hint ?? null,
				});
			}),
		);
	});

	it('rejects a denial with code/gate/message missing or non-string, or a non-string override_hint', () => {
		const corruptions: ReadonlyArray<readonly [keyof RawDenial, fc.Arbitrary<unknown>]> = [
			['code', nonStringArb],
			['gate', nonStringArb],
			['message', nonStringArb],
			['override_hint', fc.oneof(fc.integer(), fc.boolean(), fc.constant({}), fc.array(fc.string(), { maxLength: 2 }))],
		];
		const corruptionArb = fc
			.constantFrom(...corruptions)
			.chain(([field, badValueArb]) => fc.tuple(fc.constant(field), badValueArb));

		fc.assert(
			fc.property(denialEnvelopeArb, corruptionArb, ({ envelope, error }, [field, bad]) => {
				expect(parseExecutionBlockedBody({ ...envelope, error: { ...error, [field]: bad } })).toBeNull();
			}),
		);
	});

	it('rejects a denial body not wrapped in `error`, and an `error` that is not an object', () => {
		fc.assert(
			fc.property(denialArb, nonObjectArb, (error, notAnObject) => {
				// The flattened body is what a generic upstream failure might look like -- not a denial.
				expect(parseExecutionBlockedBody(error)).toBeNull();
				expect(parseExecutionBlockedBody({ error: notAnObject })).toBeNull();
			}),
		);
	});

	it('never mutates its input', () => {
		fc.assert(
			fc.property(denialEnvelopeArb, ({ envelope }) => {
				const before = structuredClone(envelope);
				parseExecutionBlockedBody(envelope);
				expect(envelope).toEqual(before);
			}),
		);
	});
});
