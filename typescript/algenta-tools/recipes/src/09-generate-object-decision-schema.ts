/**
 * Recipe 09 -- `generateObject` with a decision schema.
 *
 * Structured output is a top-three AI SDK pattern: `generateObject` + a zod schema gives you a
 * typed, validated model artifact instead of free text. This recipe makes that artifact an
 * Algenta decision proposal -- the model proposes `chosen_action` / `confidence` / `risk_p5` /
 * `rationale` in a schema-validated shape, and the proposal is then recorded in Algenta's
 * decision memory via `log_decision`, where the policy gates (`policy.min_confidence`,
 * `policy.risk_floor`) apply at execution time. The schema is the contract between the model
 * and the governance layer: no schema-conforming proposal, no logged decision.
 *
 * Run it (zero credentials -- stub engine + scripted stand-in model):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:09
 */
import { generateObject, type LanguageModel } from "ai";
import { pathToFileURL } from "node:url";
import { z } from "zod";
import { createAlgentaTools, LOG_DECISION } from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import { scriptedModel, textResponse } from "./support/scripted-model.js";

/** The decision-proposal contract: what a model must produce for its proposal to enter
 * Algenta's decision memory. `confidence` is a probability in [0, 1]; `risk_p5` is the
 * decision's 5th-percentile downside estimate in the scenario's own units -- the two fields the
 * engine's `confidence` / `risk_floor` execution gates read later. */
export const decisionProposalSchema = z.object({
  chosen_action: z.string().min(1),
  confidence: z.number().min(0).max(1),
  risk_p5: z.number(),
  rationale: z.string().min(1),
});

export type DecisionProposal = z.infer<typeof decisionProposalSchema>;

export interface ProposeAndLogResult {
  proposal: DecisionProposal;
  /** The decision-memory record id -- the handle a later `execute_decision` call uses. */
  decisionId: string;
}

/** Generates a schema-validated decision proposal for `scenario` and records it in Algenta's
 * decision memory. A model output that fails the schema never reaches `log_decision` --
 * `generateObject` rejects first. */
export async function proposeAndLogDecision(options: {
  client: AlgentaMcpClient;
  model: LanguageModel;
  scenario: string;
}): Promise<ProposeAndLogResult> {
  const { object: proposal } = await generateObject({
    model: options.model,
    schema: decisionProposalSchema,
    prompt:
      `Propose a decision for scenario "${options.scenario}". ` +
      "Return your chosen action, your confidence in [0,1], the 5th-percentile downside " +
      "estimate (risk_p5), and a one-sentence rationale.",
  });

  const tools = await createAlgentaTools({ client: options.client, profile: "govern" });
  const logged = (await tools[LOG_DECISION]!.execute!(
    {
      chosen_action: proposal.chosen_action,
      confidence: proposal.confidence,
      risk_p5: proposal.risk_p5,
    },
    directToolCallOptions(`direct-log_decision-${options.scenario}`),
  )) as { decision_id: string };

  return { proposal, decisionId: logged.decision_id };
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:09`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client, stub }) => {
    const { proposal, decisionId } = await proposeAndLogDecision({
      client,
      scenario: "launch-discount-10",
      model: scriptedModel({
        generate: [
          textResponse(
            JSON.stringify({
              chosen_action: "hold",
              confidence: 0.87,
              risk_p5: -20,
              rationale: "The discount's expected uplift does not cover its margin cost in the tail.",
            }),
          ),
        ],
      }),
    });

    console.log("Schema-validated proposal:", proposal);
    console.log(`Recorded in decision memory as ${decisionId}:`);
    console.log("  ", stub.loggedDecisions[0]);
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:09`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
