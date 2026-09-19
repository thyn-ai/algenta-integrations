/**
 * Property-based tests (fast-check) for the embedded tool-profile contract's guard,
 * `isToolAllowedForProfile` -- the check `nodes/Algenta/Algenta.node.ts` runs before every call.
 * `contract.test.ts` pins the concrete contract values; these check the invariants that must hold
 * for EVERY tool name, profile and advertised set, including the singleton-set form the node
 * itself uses.
 */
import * as fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import {
	EXECUTE_DECISION,
	FULL_PROFILE_SENTINEL,
	GET_CONTRACT,
	LOG_DECISION,
	PLAN_DECISION,
	QUERY_DATA,
	RECOMMEND,
	SIMULATE,
	TOOL_PROFILES,
	isToolAllowedForProfile,
	isToolProfile,
	type ToolProfile,
} from '../src/contract';

// ── Fixtures ──────────────────────────────────────────────────────────────────────────────────

/** Every profile the embedded contract declares, read off the module rather than re-typed here. */
const PROFILES = Object.keys(TOOL_PROFILES) as ToolProfile[];

/** The contract's escalation ladder, lowest to highest: each rung must allow a superset of the
 * rung below it. Pinned against {@link PROFILES} in the first test so the two can't drift. */
const LADDER: readonly ToolProfile[] = ['observe', 'govern', 'execute', 'full'];

const CONTRACT_TOOLS: readonly string[] = [
	GET_CONTRACT,
	QUERY_DATA,
	SIMULATE,
	RECOMMEND,
	PLAN_DECISION,
	LOG_DECISION,
	EXECUTE_DECISION,
];

/** The contract's fixed tool set for a non-`full` profile. */
function fixedSetOf(profile: ToolProfile): ReadonlySet<string> {
	const allowed = TOOL_PROFILES[profile];
	if (allowed === FULL_PROFILE_SENTINEL) {
		throw new Error(`profile ${profile} has no fixed tool set`);
	}
	return allowed;
}

const sorted = (names: Iterable<string>): string[] => [...names].sort();

// ── Arbitraries ───────────────────────────────────────────────────────────────────────────────

/** snake_case identifiers -- the shape of real MCP tool names. */
const identArb = fc.string({
	unit: fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789_'),
	minLength: 1,
	maxLength: 20,
});

const profileArb = fc.constantFrom(...PROFILES);

/** Tool names biased toward the contract's own, so a random registry regularly contains them. */
const toolNameArb = fc.oneof(fc.constantFrom(...CONTRACT_TOOLS), identArb);

/** What a connected engine might advertise: any mix of contract and unknown names. */
const advertisedArb = fc.uniqueArray(toolNameArb, { maxLength: 12 }).map((names) => new Set(names));

// ── isToolAllowedForProfile ───────────────────────────────────────────────────────────────────

describe('isToolAllowedForProfile (properties)', () => {
	it('the ladder used below names exactly the contract’s profiles', () => {
		expect(sorted(LADDER)).toEqual(sorted(PROFILES));
	});

	it('never allows a tool the engine does not advertise, under any profile', () => {
		fc.assert(
			fc.property(toolNameArb, profileArb, advertisedArb, (toolName, profile, advertised) => {
				advertised.delete(toolName);
				expect(isToolAllowedForProfile(toolName, profile, advertised)).toBe(false);
			}),
		);
	});

	it('full allows exactly what the engine advertises', () => {
		fc.assert(
			fc.property(toolNameArb, advertisedArb, (toolName, advertised) => {
				expect(isToolAllowedForProfile(toolName, 'full', advertised)).toBe(advertised.has(toolName));
			}),
		);
	});

	it('a non-full profile allows exactly its fixed contract set intersected with what is advertised', () => {
		fc.assert(
			fc.property(
				toolNameArb,
				profileArb.filter((profile) => profile !== 'full'),
				advertisedArb,
				(toolName, profile, advertised) => {
					expect(isToolAllowedForProfile(toolName, profile, advertised)).toBe(
						advertised.has(toolName) && fixedSetOf(profile).has(toolName),
					);
				},
			),
		);
	});

	it('profiles form a ladder: allowed under one rung implies allowed under every higher rung', () => {
		fc.assert(
			fc.property(toolNameArb, advertisedArb, (toolName, advertised) => {
				const rungs = LADDER.map((profile) => isToolAllowedForProfile(toolName, profile, advertised));
				for (let i = 1; i < rungs.length; i++) {
					if (rungs[i - 1]) {
						expect(rungs[i]).toBe(true);
					}
				}
			}),
		);
	});

	it('execute_decision is reachable only through the execute and full profiles', () => {
		fc.assert(
			fc.property(profileArb, advertisedArb, (profile, advertised) => {
				advertised.add(EXECUTE_DECISION);
				expect(isToolAllowedForProfile(EXECUTE_DECISION, profile, advertised)).toBe(
					profile === 'execute' || profile === 'full',
				);
			}),
		);
	});

	it('observe never allows a govern- or execute-tier tool, whatever the engine advertises', () => {
		fc.assert(
			fc.property(advertisedArb, (advertised) => {
				for (const tier of [PLAN_DECISION, LOG_DECISION, EXECUTE_DECISION]) {
					advertised.add(tier);
					expect(isToolAllowedForProfile(tier, 'observe', advertised)).toBe(false);
				}
			}),
		);
	});

	it('with the singleton set the node passes, the guard reduces to the fixed contract boundary', () => {
		// `Algenta.node.ts` calls `isToolAllowedForProfile(operation, profile, new Set([operation]))`:
		// the profile boundary and nothing else must decide, for every operation name.
		fc.assert(
			fc.property(toolNameArb, profileArb, (operation, profile) => {
				const expected = profile === 'full' || fixedSetOf(profile).has(operation);
				expect(isToolAllowedForProfile(operation, profile, new Set([operation]))).toBe(expected);
			}),
		);
	});
});

// ── isToolProfile ─────────────────────────────────────────────────────────────────────────────

describe('isToolProfile (properties)', () => {
	/** Arbitrary strings plus near misses: a real profile with its case, whitespace or last
	 * character perturbed. Each must be rejected -- the contract is matched byte for byte. */
	const candidateArb = fc.oneof(
		fc.string(),
		profileArb,
		profileArb.map((profile) => profile.toUpperCase()),
		profileArb.map((profile) => ` ${profile}`),
		profileArb.map((profile) => `${profile} `),
		profileArb.map((profile) => profile.slice(0, -1)),
	);

	it('is total and accepts exactly the contract’s profiles', () => {
		fc.assert(
			fc.property(candidateArb, (value) => {
				expect(isToolProfile(value)).toBe((PROFILES as string[]).includes(value));
			}),
		);
	});
});
