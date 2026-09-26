/**
 * Recipe 07 -- Streaming governed turns.
 *
 * Streaming is the AI SDK's flagship surface (`streamText` -> `useChat` in every Next.js AI
 * app). This recipe consumes a governed turn's `fullStream` part-by-part and extracts the two
 * Algenta artifacts that matter for UI and audit: the typed `ExecutionReceipt` riding in a
 * `tool-result` part, and the engine's structured denial riding in a `tool-error` part (an
 * `ExecutionBlockedError` with its named gate -- the same error that would be thrown by a
 * direct call, delivered here as a stream part so the model turn can continue).
 *
 * Run it (zero credentials -- stub engine + scripted stand-in model):
 *
 *   cd typescript/algenta-tools && pnpm --filter algenta-tools-recipes recipe:07
 */
import { stepCountIs, streamText, type LanguageModel, type TextStreamPart } from "ai";
import { pathToFileURL } from "node:url";
import {
  createAlgentaTools,
  EXECUTE_DECISION,
  ExecutionBlockedError,
  parseExecutionReceipt,
  type ExecutionReceipt,
  type ToolProfile,
} from "algenta-tools";

import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import {
  scriptedModel,
  textStreamParts,
  toolCallStreamPart,
} from "./support/scripted-model.js";
import { LOG_DECISION } from "algenta-tools";

export interface GovernedStreamEvent {
  type: string;
  /** Present on tool-call / tool-result / tool-error parts. */
  toolName?: string;
}

export interface GovernedDenial {
  toolName: string;
  gate: string | null;
  code: string | null;
}

export interface GovernedStreamSummary {
  text: string;
  /** Every fullStream part type, in order -- the transcript a UI would render from. */
  events: GovernedStreamEvent[];
  /** Typed receipts lifted out of `tool-result` parts. */
  receipts: ExecutionReceipt[];
  /** Structured denials lifted out of `tool-error` parts, with the engine's named gate. */
  denials: GovernedDenial[];
}

/** Reads the named gate out of a `tool-error` part's error without assuming its class: an
 * `ExecutionBlockedError` instance, or the same fields on a serialized cross-boundary copy. */
export function readGateFromToolError(error: unknown): { gate: string | null; code: string | null } {
  if (error instanceof ExecutionBlockedError) {
    return { gate: error.gate, code: error.code };
  }
  if (typeof error === "object" && error !== null) {
    const record = error as Record<string, unknown>;
    return {
      gate: typeof record.gate === "string" ? record.gate : null,
      code: typeof record.code === "string" ? record.code : null,
    };
  }
  return { gate: null, code: null };
}

/** Streams one governed turn, collecting the typed Algenta artifacts out of the stream parts. */
export async function streamGovernedTurn(options: {
  client: AlgentaMcpClient;
  model: LanguageModel;
  prompt: string;
  profile?: ToolProfile;
  maxSteps?: number;
}): Promise<GovernedStreamSummary> {
  const tools = await createAlgentaTools({ client: options.client, profile: options.profile ?? "execute" });
  const result = streamText({
    model: options.model,
    tools,
    stopWhen: stepCountIs(options.maxSteps ?? 4),
    prompt: options.prompt,
  });

  const events: GovernedStreamEvent[] = [];
  const receipts: ExecutionReceipt[] = [];
  const denials: GovernedDenial[] = [];

  for await (const part of result.fullStream as AsyncIterable<TextStreamPart<typeof tools>>) {
    const event: GovernedStreamEvent = { type: part.type };
    if (part.type === "tool-call" || part.type === "tool-result" || part.type === "tool-error") {
      event.toolName = part.toolName;
    }
    events.push(event);

    if (part.type === "tool-result") {
      const receipt = parseExecutionReceipt(part.output);
      if (receipt !== null) {
        receipts.push(receipt);
      }
    } else if (part.type === "tool-error") {
      const { gate, code } = readGateFromToolError(part.error);
      denials.push({ toolName: part.toolName, gate, code });
    }
  }

  return { text: await result.text, events, receipts, denials };
}

/** Standalone runner: `pnpm --filter algenta-tools-recipes recipe:07`. Streams one successful
 * execution turn and one policy-denied turn. */
export async function main(): Promise<void> {
  await withStubAlgenta(async ({ client }) => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const goodId = (
      (await tools[LOG_DECISION]!.execute!(
        { chosen_action: "ship_it", confidence: 0.92 },
        directToolCallOptions("direct-log-good"),
      )) as { decision_id: string }
    ).decision_id;
    const lowConfidenceId = (
      (await tools[LOG_DECISION]!.execute!(
        { chosen_action: "ship_it", confidence: 0.1 },
        directToolCallOptions("direct-log-low"),
      )) as { decision_id: string }
    ).decision_id;

    console.log("1. Streaming a successful execution turn:");
    const success = await streamGovernedTurn({
      client,
      model: scriptedModel({
        stream: [
          toolCallStreamPart(EXECUTE_DECISION, { decision_id: goodId, webhook_url: "https://example.com/h" }),
          textStreamParts("Executed; receipt delivered."),
        ],
      }),
      prompt: "execute the good decision",
    });
    console.log("   events:", success.events.map(e => e.type).join(" -> "));
    console.log("   receipts:", success.receipts.map(r => `${r.decision_id}:${r.execution_status}`));

    console.log("2. Streaming a policy-denied execution turn (confidence gate):");
    const denied = await streamGovernedTurn({
      client,
      model: scriptedModel({
        stream: [
          toolCallStreamPart(EXECUTE_DECISION, { decision_id: lowConfidenceId, webhook_url: "https://example.com/h" }),
          textStreamParts("The engine blocked it; stopping."),
        ],
      }),
      prompt: "execute the low-confidence decision",
    });
    console.log("   events:", denied.events.map(e => e.type).join(" -> "));
    console.log("   denials:", denied.denials);
  });
}

/* v8 ignore next 3 -- standalone entrypoint; covered via `pnpm recipe:07`, not the unit suite. */
if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  await main();
}
