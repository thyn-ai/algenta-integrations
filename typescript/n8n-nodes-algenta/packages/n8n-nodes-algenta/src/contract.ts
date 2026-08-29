/**
 * The tool-profile contract, embedded for runtime use.
 *
 * This is a runtime-embedded copy of the profile/tool-name boundaries defined in
 * `contracts/integration-tool-contract.json` at the root of the `algenta-integrations`
 * repository -- duplicated (not imported across the workspace boundary) for the same reason
 * `algenta-tools/src/contract.ts` duplicates it: once this package is installed from npm, the
 * JSON contract file living in its source monorepo is not on disk anymore.
 *
 * This is not a license to invent a different boundary. `test/contract-parity.test.ts` loads the
 * real contract file (when running inside a checkout of `algenta-integrations`) and asserts these
 * constants agree with it on the tool-name sets.
 */

export type ToolProfile = 'observe' | 'govern' | 'execute' | 'full';

export const DEFAULT_PROFILE: ToolProfile = 'observe';

export const GET_CONTRACT = 'get_contract';
export const QUERY_DATA = 'query_data';
export const SIMULATE = 'simulate';
export const RECOMMEND = 'recommend';
export const PLAN_DECISION = 'plan_decision';
export const LOG_DECISION = 'log_decision';
export const EXECUTE_DECISION = 'execute_decision';

const OBSERVE_TOOLS: ReadonlySet<string> = new Set([GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND]);
const GOVERN_TOOLS: ReadonlySet<string> = new Set([...OBSERVE_TOOLS, PLAN_DECISION, LOG_DECISION]);
const EXECUTE_TOOLS: ReadonlySet<string> = new Set([...GOVERN_TOOLS, EXECUTE_DECISION]);

export const FULL_PROFILE_SENTINEL = '*' as const;

export const TOOL_PROFILES: Readonly<Record<ToolProfile, ReadonlySet<string> | typeof FULL_PROFILE_SENTINEL>> = {
	observe: OBSERVE_TOOLS,
	govern: GOVERN_TOOLS,
	execute: EXECUTE_TOOLS,
	full: FULL_PROFILE_SENTINEL,
};

/** Fields on execute_decision's real schema that are operator/break-glass-only. The Algenta node
 * never exposes these as user-fillable parameters on ANY operation, in ANY profile -- see
 * `nodes/Algenta/Algenta.node.ts`'s Execute Decision properties, which simply do not declare
 * `force`/`override_safety` fields at all. Listed here (rather than only enforced by omission) so
 * a future operation can check against it explicitly instead of relying on absence alone. */
export const NEVER_MODEL_FACING_FIELDS: ReadonlySet<string> = new Set(['force', 'override_safety']);

const KNOWN_TOOL_PROFILES: ReadonlySet<string> = new Set(Object.keys(TOOL_PROFILES));

export function isToolProfile(value: string): value is ToolProfile {
	return KNOWN_TOOL_PROFILES.has(value);
}

/** True if `toolName` is allowed under `profile`, given what the connected engine actually
 * advertises right now. `full` allows anything the engine advertises; the other three profiles
 * are the contract's fixed set intersected with what's actually available. */
export function isToolAllowedForProfile(
	toolName: string,
	profile: ToolProfile,
	availableToolNames: ReadonlySet<string>,
): boolean {
	if (!availableToolNames.has(toolName)) {
		return false;
	}
	const allowed = TOOL_PROFILES[profile];
	return allowed === FULL_PROFILE_SENTINEL || allowed.has(toolName);
}
