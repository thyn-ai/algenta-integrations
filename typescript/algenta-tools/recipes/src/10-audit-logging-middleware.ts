/**
 * Recipe 10 -- Audit-logging language-model middleware.
 *
 * `wrapLanguageModel` middleware is the AI SDK's official cross-cutting hook (logging,
 * guardrails, caching) -- the place teams put "record every model call" plumbing. This recipe
 * builds the audit middleware a governed deployment wants: every generate/stream call is
 * recorded with the tool profile in force and the exact tool names the model was offered, next
 * to the finish reason. Algenta's execution receipts answer "what was executed"; this audit
 * trail answers the question before it -- "what was the model allowed to do, and what did it
 * ask for". The middleware is pure observation: it never alters the model call or its result.
 *
 * Run it (zero credentials -- stub engine + scripted stand-in model):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:10
 */
import {
  generateText,
  stepCountIs,
  wrapLanguageModel,
  type LanguageModel,
  type LanguageModelMiddleware,
} from "ai";
import type { LanguageModelV4CallOptions } from "@ai-sdk/provider";
import { pathToFileURL } from "node:url";
import { createAlgentaTools, SIMULATE, type ToolProfile } from "algenta-tools";

import { withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";

export interface AuditEntry {
  /** ISO-8601 timestamp from the injected clock (deterministic under test). */
  at: string;
  /** Which Algenta tool profile the toolset in play was built with -- the governance context
   * an auditor needs next to the call itself. */
  profile: ToolProfile;
  kind: "generate" | "stream";
  phase: "start" | "finish";
  modelId: string;
  /** The exact tool names offered to the model on this call, in order. */
  offeredToolNames: string[];
  promptMessages: number;
  /** `finish` entries only: the call's unified finish reason (e.g. "stop", "tool-calls"). */
  finishReason?: string;
}

/** Pulls the audit-relevant fields out of the real call params, without assuming tools exist. */
function describeParams(params: LanguageModelV4CallOptions): {
  offeredToolNames: string[];
  promptMessages: number;
} {
  return {
    offeredToolNames: (params.tools ?? []).map(tool => tool.name),
    promptMessages: params.prompt.length,
  };
}

/** Builds a `LanguageModelMiddleware` that appends one entry per call start and per call finish
 * to `sink`. `clock` is injected for determinism (defaults to the real wall clock -- the one
 * place an audit trail legitimately wants wall time). */
export function createAuditMiddleware(options: {
  sink: AuditEntry[];
  profile: ToolProfile;
  clock?: () => Date;
}): LanguageModelMiddleware {
  const clock = options.clock ?? (() => new Date());
  const record = (entry: Omit<AuditEntry, "at" | "profile">) => {
    options.sink.push({ at: clock().toISOString(), profile: options.profile, ...entry });
  };

  return {
    specificationVersion: "v4",
    wrapGenerate: async ({ doGenerate, params, model }) => {
      record({ kind: "generate", phase: "start", modelId: model.modelId, ...describeParams(params) });
      const result = await doGenerate();
      record({
        kind: "generate",
        phase: "finish",
        modelId: model.modelId,
        ...describeParams(params),
        finishReason: result.finishReason.unified,
      });
      return result;
    },
    wrapStream: async ({ doStream, params, model }) => {
      record({ kind: "stream", phase: "start", modelId: model.modelId, ...describeParams(params) });
      const result = await doStream();
      const described = describeParams(params);
      const observed = new TransformStream({
        transform(part, controller) {
          if (part.type === "finish") {
            record({
              kind: "stream",
              phase: "finish",
              modelId: model.modelId,
              ...described,
              finishReason: part.finishReason.unified,
            });
          }
          controller.enqueue(part);
        },
      });
      return { ...result, stream: result.stream.pipeThrough(observed) };
    },
  };
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:10`. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client }) => {
    const sink: AuditEntry[] = [];
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const model = wrapLanguageModel({
      model: scriptedModel({
        generate: [toolCallResponse(SIMULATE, { scenario: "launch-discount-10" }), textResponse("EV is 42.0.")],
      }),
      middleware: createAuditMiddleware({ sink, profile: "observe" }),
    });

    const result = await generateText({
      model,
      tools,
      stopWhen: stepCountIs(4),
      prompt: "simulate launch-discount-10 and report",
    });

    console.log(`Answer: ${result.text}`);
    console.log("Audit trail:");
    for (const entry of sink) {
      console.log(
        `  [${entry.at}] ${entry.kind}/${entry.phase} model=${entry.modelId} ` +
          `tools=[${entry.offeredToolNames.join(",")}] finish=${entry.finishReason ?? "-"}`,
      );
    }
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:10`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
