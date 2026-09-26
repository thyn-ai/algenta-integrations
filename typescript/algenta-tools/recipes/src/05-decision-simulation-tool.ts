/**
 * Recipe 05 -- Decision simulation as an AI SDK tool.
 *
 * A pattern straight out of every "advanced tools" cookbook: compose several backend calls into
 * one deterministic, model-callable tool. Here the composition is Algenta's governed
 * decision-making flow itself -- `simulate` (expected value) + `recommend` (action, confidence)
 * + `log_decision` (persist to decision memory) -- exposed to the agent as a single
 * `algenta_simulate_decision` tool. One tool call gives the model a simulated, recommended,
 * and *recorded* decision, with the `decision_id` it can later pass to `execute_decision`.
 *
 * Run it (zero credentials -- stub engine + scripted stand-in model):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:05
 */
import { generateText, stepCountIs, tool, type Tool } from "ai";
import { pathToFileURL } from "node:url";
import { z } from "zod";
import {
  createAlgentaTools,
  LOG_DECISION,
  RECOMMEND,
  SIMULATE,
  type AlgentaToolSet,
} from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";

/** The fields this recipe reads out of each freeform tool payload, schema-validated rather than
 * assumed -- a payload that doesn't carry them is an explicit error, not a silent `undefined`. */
const simulatePayloadSchema = z.object({ expected_value: z.number() }).passthrough();
const recommendPayloadSchema = z
  .object({ recommended_action: z.string(), confidence: z.number() })
  .passthrough();
const logDecisionPayloadSchema = z.object({ decision_id: z.string() }).passthrough();

export interface SimulatedDecision {
  decisionId: string;
  scenario: string;
  expectedValue: number;
  recommendedAction: string;
  confidence: number;
}

/** Builds the composite `algenta_simulate_decision` tool over a govern-tier (or higher)
 * `AlgentaToolSet`. Every call is exactly three engine calls, in a fixed order -- deterministic
 * given the engine's responses, and every decision persisted to decision memory as a side
 * effect the caller can audit. */
export function createDecisionSimulationTool(options: { tools: AlgentaToolSet }): Tool {
  const { tools } = options;
  for (const required of [SIMULATE, RECOMMEND, LOG_DECISION] as const) {
    if (!tools[required]?.execute) {
      throw new Error(
        `${required} is not in this ToolSet -- build it with profile 'govern' (or higher) first.`,
      );
    }
  }

  return tool({
    description:
      "Simulate a decision scenario with Algenta (expected value), get Algenta's recommended " +
      "action and confidence, and record the decision in Algenta's decision memory. Returns " +
      "the decision_id for later execution.",
    inputSchema: z.object({ scenario: z.string().min(1) }),
    execute: async ({ scenario }): Promise<SimulatedDecision> => {
      const simulated = simulatePayloadSchema.parse(
        await tools[SIMULATE]!.execute!({ scenario }, directToolCallOptions(`direct-simulate-${scenario}`)),
      );
      const recommended = recommendPayloadSchema.parse(
        await tools[RECOMMEND]!.execute!({ scenario }, directToolCallOptions(`direct-recommend-${scenario}`)),
      );
      const logged = logDecisionPayloadSchema.parse(
        await tools[LOG_DECISION]!.execute!(
          {
            chosen_action: recommended.recommended_action,
            confidence: recommended.confidence,
            expected_value: simulated.expected_value,
          },
          directToolCallOptions(`direct-log_decision-${scenario}`),
        ),
      );
      return {
        decisionId: logged.decision_id,
        scenario,
        expectedValue: simulated.expected_value,
        recommendedAction: recommended.recommended_action,
        confidence: recommended.confidence,
      };
    },
  });
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:05`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client, stub }) => {
    const governTools = await createAlgentaTools({ client, profile: "govern" });
    const tools = { algenta_simulate_decision: createDecisionSimulationTool({ tools: governTools }) };

    const result = await generateText({
      model: scriptedModel({
        generate: [
          toolCallResponse("algenta_simulate_decision", { scenario: "launch-discount-10" }),
          textResponse('Simulated EV is 42.0; recommendation "hold" (confidence 0.87) is logged.'),
        ],
      }),
      tools,
      stopWhen: stepCountIs(4),
      prompt: 'Simulate and record a decision for scenario "launch-discount-10".',
    });

    console.log(`Agent answer: ${result.text}`);
    console.log("Decisions persisted in Algenta decision memory:");
    for (const record of stub.loggedDecisions) {
      console.log(
        `  ${record.decision_id}  action=${record.chosen_action} confidence=${record.confidence} ev=${record.expected_value}`,
      );
    }
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:05`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
