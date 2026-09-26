/**
 * Recipe 01 -- Governed tool calls in an AI SDK agent (`ToolLoopAgent`).
 *
 * The single most popular Vercel AI SDK pattern: an agent looping over model turns and tool
 * calls until the task is done. This recipe builds that agent with an Algenta-governed
 * `ToolSet` from `createAlgentaTools`, so the model's tool surface is exactly the requested
 * tool profile -- nothing write-tier or execute-tier is even visible to the model unless the
 * operator opted into that profile -- with `force`/`override_safety` scrubbed from every
 * advertised schema in every profile.
 *
 * Run it (zero credentials -- stub engine + scripted stand-in model):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:01
 *
 * Swap the stub client's `baseUrl` for a self-hosted engine and the scripted model for any real
 * AI SDK provider model and the same code is the real thing.
 */
import { stepCountIs, ToolLoopAgent, type LanguageModel } from "ai";
import { pathToFileURL } from "node:url";
import {
  createAlgentaTools,
  DEFAULT_PROFILE,
  RECOMMEND,
  SIMULATE,
  type AlgentaToolSet,
  type ToolProfile,
} from "algenta-tools";

import { withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";

export interface CreateGovernedAgentOptions {
  client: AlgentaMcpClient;
  model: LanguageModel;
  /** Which contract profile the agent's tool surface is filtered to. Defaults to `"observe"`
   * (read-only), same as `createAlgentaTools`. */
  profile?: ToolProfile;
  instructions?: string;
  /** Loop stop condition: at most this many model+tool steps. Defaults to 4. */
  maxSteps?: number;
}

export const DEFAULT_AGENT_INSTRUCTIONS =
  "You are a careful decision-support analyst. Ground every quantitative claim in a real " +
  "Algenta tool result; never invent numbers the tools did not return.";

/** Builds a `ToolLoopAgent` whose tool surface is the Algenta-governed `ToolSet` for `profile`. */
export async function createGovernedAgent(
  options: CreateGovernedAgentOptions,
): Promise<{ agent: ToolLoopAgent<never, AlgentaToolSet>; tools: AlgentaToolSet; profile: ToolProfile }> {
  const profile = options.profile ?? DEFAULT_PROFILE;
  const tools = await createAlgentaTools({ client: options.client, profile });
  const agent = new ToolLoopAgent({
    model: options.model,
    tools,
    instructions: options.instructions ?? DEFAULT_AGENT_INSTRUCTIONS,
    stopWhen: stepCountIs(options.maxSteps ?? 4),
  });
  return { agent, tools, profile };
}

export interface GovernedToolCall {
  toolName: string;
  output: unknown;
}

export interface GovernedAgentTurnSummary {
  profile: ToolProfile;
  text: string;
  stepCount: number;
  toolCalls: GovernedToolCall[];
}

/** Runs one governed agent turn and summarizes which tools the loop actually called, with the
 * real (stub-engine-backed) payloads each returned. */
export async function runGovernedAgentTurn(
  options: CreateGovernedAgentOptions & { prompt: string },
): Promise<GovernedAgentTurnSummary> {
  const { agent, profile } = await createGovernedAgent(options);
  const result = await agent.generate({ prompt: options.prompt });
  const toolCalls: GovernedToolCall[] = [];
  for (const step of result.steps) {
    for (const part of step.content) {
      if (part.type === "tool-result") {
        toolCalls.push({ toolName: part.toolName, output: part.output });
      }
    }
  }
  return { profile, text: result.text, stepCount: result.steps.length, toolCalls };
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:01`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client }) => {
    const scenario = "launch-discount-10";
    const model = scriptedModel({
      generate: [
        toolCallResponse(SIMULATE, { scenario }),
        toolCallResponse(RECOMMEND, { scenario }),
        textResponse(`For ${scenario}: expected value 42.0, recommended action "hold" (confidence 0.87).`),
      ],
    });
    const summary = await runGovernedAgentTurn({
      client,
      model,
      prompt: `Evaluate scenario "${scenario}": simulate it, get a recommendation, then summarize.`,
    });
    console.log(`Profile: ${summary.profile} (read-only -- no write/execute tool exists in this toolset)`);
    console.log(`Steps: ${summary.stepCount}`);
    for (const call of summary.toolCalls) {
      console.log(`  tool ${call.toolName} ->`, call.output);
    }
    console.log(`Agent answer: ${summary.text}`);
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:01`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
