/**
 * The tool-profile contract, embedded for runtime use.
 *
 * This module is a *runtime-embedded copy* of the profile/tool-name boundaries defined in
 * {@link ../../../../contracts/integration-tool-contract.json} at the root of the
 * `algenta-integrations` repository. It has to be embedded rather than read from that file at
 * import time: once this package is installed from npm, the JSON contract file living in its
 * source monorepo is not on disk anymore, so the constants below are this package's copy of
 * record.
 *
 * This is *not* a license to invent a different boundary. `src/contract-parity.test.ts` loads
 * the real contract file (when running inside a checkout of `algenta-integrations`, i.e. in this
 * repository's own CI) and asserts, exactly on the tool-name sets, that these constants agree
 * with it. If the contract file changes, that test fails here until this module is updated to
 * match -- so drift between the contract and this package is a test failure, not a silent
 * possibility.
 */

/** The four tool profiles defined by the shared tool-profile contract.
 *
 * See `contracts/integration-tool-contract.json` in the repository root for the authoritative
 * description of each. `"observe"` is the default for every integration package in this
 * repository unless a caller explicitly opts into a higher profile.
 */
export type ToolProfile = "observe" | "govern" | "execute" | "full";

export const DEFAULT_PROFILE: ToolProfile = "observe";

// The full MCP tool-registry names referenced by the contract's per-profile tool lists (the
// "full" profile additionally exposes everything else the connected engine's MCP tool registry
// advertises -- this package cannot enumerate that ahead of time since it varies by engine
// version and deployment, hence "full"'s `tools: "*"` in the contract).
export const GET_CONTRACT = "get_contract";
export const QUERY_DATA = "query_data";
export const SIMULATE = "simulate";
export const RECOMMEND = "recommend";
export const PLAN_DECISION = "plan_decision";
export const LOG_DECISION = "log_decision";
export const EXECUTE_DECISION = "execute_decision";

const OBSERVE_TOOLS: ReadonlySet<string> = new Set([GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND]);
const GOVERN_TOOLS: ReadonlySet<string> = new Set([...OBSERVE_TOOLS, PLAN_DECISION, LOG_DECISION]);
const EXECUTE_TOOLS: ReadonlySet<string> = new Set([...GOVERN_TOOLS, EXECUTE_DECISION]);

/** `"full"` sentinel: every tool the connected engine's MCP registry advertises, opt-in only.
 * Mirrors the contract's `profiles.full.tools == "*"`. */
export const FULL_PROFILE_SENTINEL = "*" as const;

export const TOOL_PROFILES: Readonly<Record<ToolProfile, ReadonlySet<string> | typeof FULL_PROFILE_SENTINEL>> = {
  observe: OBSERVE_TOOLS,
  govern: GOVERN_TOOLS,
  execute: EXECUTE_TOOLS,
  full: FULL_PROFILE_SENTINEL,
};

/** Fields that exist on `execute_decision`'s schema for operator/break-glass use only. No
 * profile, and no tool-name filtering, may turn these into a model-facing parameter -- see the
 * contract's `profiles.execute.never_model_facing_note`. `createAlgentaTools` enforces this at
 * two layers: it strips these keys from a tool's advertised JSON schema *and* from the arguments
 * object actually forwarded to the wrapped MCP tool call, so a model that somehow still emitted
 * one of these as an argument (e.g. by copying it from a prior message) can't get it through
 * either.
 */
export const NEVER_MODEL_FACING_FIELDS: ReadonlySet<string> = new Set(["force", "override_safety"]);

const KNOWN_TOOL_PROFILES: ReadonlySet<string> = new Set(Object.keys(TOOL_PROFILES));

export function isToolProfile(value: string): value is ToolProfile {
  return KNOWN_TOOL_PROFILES.has(value);
}

/**
 * Resolve a profile to the concrete tool names it allows, given what the server advertises.
 *
 * For `"full"`, that's every name the connected engine's MCP registry advertises right now
 * (`availableToolNames`, unfiltered). For the other three profiles, it's the contract's fixed set
 * intersected with what's actually available -- a tool the contract names but the connected
 * engine doesn't currently advertise is simply absent, not an error.
 */
export function resolveProfileToolNames(
  profile: ToolProfile,
  availableToolNames: Iterable<string>,
): Set<string> {
  const available = new Set(availableToolNames);
  const allowed = TOOL_PROFILES[profile];
  if (allowed === FULL_PROFILE_SENTINEL) {
    return available;
  }
  const result = new Set<string>();
  for (const name of allowed) {
    if (available.has(name)) {
      result.add(name);
    }
  }
  return result;
}
