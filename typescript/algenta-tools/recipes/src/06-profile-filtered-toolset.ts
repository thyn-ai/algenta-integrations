/**
 * Recipe 06 -- Profile-filtered toolsets (observe / govern / execute / full).
 *
 * Least-privilege tool exposure is the standard multi-agent / multi-tenant pattern on the AI
 * SDK: different agents (or different routes) get deliberately different tool surfaces. With
 * Algenta the ladder is the tool-profile contract itself, enforced by `createAlgentaTools` on
 * the real MCP wire: an analyst agent built on the `observe` profile does not merely avoid
 * calling `execute_decision` -- the tool is never in its `ToolSet`, so the model cannot know it
 * exists. `force`/`override_safety` are scrubbed from every advertised schema *and* from the
 * arguments forwarded to the engine, in every profile.
 *
 * Run it (zero credentials -- stub engine):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:06
 */
import { asSchema } from "ai";
import { pathToFileURL } from "node:url";
import {
  createAlgentaTools,
  EXECUTE_DECISION,
  type AlgentaToolSet,
  type ToolProfile,
} from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";

/** A role -> least-privilege profile map, the way an app would assign tool surfaces to its
 * agents. Additive ladder: each role can do everything the previous one can, plus one tier. */
export const ROLE_PROFILES = {
  /** Read-only analysis: get_contract, query_data, simulate, recommend. */
  analyst: "observe",
  /** + propose/record decisions: plan_decision, log_decision. Never executes. */
  planner: "govern",
  /** + real-world execution: execute_decision. */
  operator: "execute",
  /** Everything the connected engine advertises, including non-contract admin tools. Opt-in. */
  admin: "full",
} as const satisfies Record<string, ToolProfile>;

export type ToolRole = keyof typeof ROLE_PROFILES;

/** Builds one governed `ToolSet` per role over a single shared MCP connection. */
export async function createRoleToolsets(
  client: AlgentaMcpClient,
): Promise<Record<ToolRole, AlgentaToolSet>> {
  const entries: Array<readonly [ToolRole, AlgentaToolSet]> = [];
  for (const [role, profile] of Object.entries(ROLE_PROFILES) as Array<[ToolRole, ToolProfile]>) {
    entries.push([role, await createAlgentaTools({ client, profile })] as const);
  }
  return Object.fromEntries(entries) as Record<ToolRole, AlgentaToolSet>;
}

/** The property names of a tool's advertised input schema -- what the model is told it may
 * pass. Async because `asSchema(...).jsonSchema` resolves lazily for deferred schemas. */
export async function advertisedInputProperties(
  toolset: AlgentaToolSet,
  toolName: string,
): Promise<string[]> {
  const target = toolset[toolName];
  if (!target) {
    throw new Error(`Tool '${toolName}' is not in this ToolSet.`);
  }
  const schema = (await asSchema(target.inputSchema).jsonSchema) as {
    properties?: Record<string, unknown>;
  };
  return Object.keys(schema.properties ?? {});
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:06`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client, stub }) => {
    const toolsets = await createRoleToolsets(client);

    console.log("Least-privilege tool surfaces per role:");
    for (const [role, toolset] of Object.entries(toolsets)) {
      console.log(`  ${role.padEnd(8)} (${ROLE_PROFILES[role as ToolRole]}): ${Object.keys(toolset).join(", ")}`);
    }

    const executeSchemaProps = await advertisedInputProperties(toolsets.operator, EXECUTE_DECISION);
    console.log(`\noperator's execute_decision advertised properties: ${executeSchemaProps.join(", ")}`);
    console.log("(force / override_safety are operator-only and never advertised to a model)");

    // And even if a hand-rolled caller sneaks one into the arguments object, it is scrubbed
    // before the engine ever sees it -- inspect what the engine actually received:
    await toolsets.operator[EXECUTE_DECISION]!.execute!(
      { decision_id: "decision-x", webhook_url: "https://example.com/h", force: true, override_safety: true },
      directToolCallOptions("direct-scrub-demo"),
    );
    console.log("\nWhat the engine received for that call:", stub.executeDecisionCalls[0]);
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:06`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
