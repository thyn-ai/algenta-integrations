/**
 * Deterministic, credential-free language-model scripting for the recipes.
 *
 * Every recipe that drives a real AI SDK loop (`generateText` / `streamText` / `ToolLoopAgent` /
 * `generateObject`) runs it against a `MockLanguageModelV4` from `ai/test` -- the AI SDK's own
 * test double -- scripted with an exact sequence of `doGenerate`/`doStream` responses. The tool
 * loop, tool-call parsing, stop conditions, streaming transforms, and middleware are all the
 * real AI SDK machinery; only the provider HTTP call (the one thing that would need an API key)
 * is replaced. The Algenta side of every recipe still goes over the real MCP wire to the stub
 * server (`./stub-server.ts`), so nothing about the governance behavior is faked. The recipe
 * test suites and their standalone runners (`pnpm recipe:NN`) both use this; swap the scripted
 * model for any real AI SDK provider model and the same recipe code is the real thing.
 */
import type {
  LanguageModelV4CallOptions,
  LanguageModelV4GenerateResult,
  LanguageModelV4StreamPart,
  LanguageModelV4StreamResult,
  LanguageModelV4Usage,
} from "@ai-sdk/provider";
import { convertArrayToReadableStream, MockLanguageModelV4 } from "ai/test";

/** A fixed, well-formed usage payload -- deterministic, and shaped exactly like the real
 * `LanguageModelV4Usage` the AI SDK aggregates. */
export const SCRIPTED_USAGE: LanguageModelV4Usage = {
  inputTokens: { total: 12, noCache: 12, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 4, text: 4, reasoning: 0 },
};

/** A `doGenerate` response whose content is one tool call (`input` is the stringified-JSON
 * arguments, per the real `LanguageModelV4ToolCall` contract). */
export function toolCallResponse(
  toolName: string,
  args: Record<string, unknown>,
  toolCallId = `call-${toolName}-1`,
): LanguageModelV4GenerateResult {
  return {
    content: [{ type: "tool-call", toolCallId, toolName, input: JSON.stringify(args) }],
    finishReason: { unified: "tool-calls", raw: "tool-calls" },
    usage: SCRIPTED_USAGE,
    warnings: [],
  };
}

/** A `doGenerate` response whose content is one plain text block, finishing the step. */
export function textResponse(text: string): LanguageModelV4GenerateResult {
  return {
    content: [{ type: "text", text }],
    finishReason: { unified: "stop", raw: "stop" },
    usage: SCRIPTED_USAGE,
    warnings: [],
  };
}

/** The streamed equivalent of {@link toolCallResponse}: a real
 * `LanguageModelV4StreamPart` sequence carrying one complete tool call. */
export function toolCallStreamPart(
  toolName: string,
  args: Record<string, unknown>,
  toolCallId = `call-${toolName}-1`,
): LanguageModelV4StreamPart[] {
  return [
    { type: "stream-start", warnings: [] },
    { type: "tool-call", toolCallId, toolName, input: JSON.stringify(args) },
    {
      type: "finish",
      finishReason: { unified: "tool-calls", raw: "tool-calls" },
      usage: SCRIPTED_USAGE,
    },
  ];
}

/** The streamed equivalent of {@link textResponse}. */
export function textStreamParts(text: string): LanguageModelV4StreamPart[] {
  return [
    { type: "stream-start", warnings: [] },
    { type: "text-start", id: "text-1" },
    { type: "text-delta", id: "text-1", delta: text },
    { type: "text-end", id: "text-1" },
    { type: "finish", finishReason: { unified: "stop", raw: "stop" }, usage: SCRIPTED_USAGE },
  ];
}

export interface ScriptedModelOptions {
  /** `doGenerate` responses, consumed in order (one per model call the loop makes). */
  generate?: LanguageModelV4GenerateResult[];
  /** `doStream` responses, consumed in order. */
  stream?: LanguageModelV4StreamPart[][];
  /** Optional observer, called with the exact call options the AI SDK sent the model on every
   * `doGenerate`/`doStream` -- lets a test assert on what the loop asked the model for. */
  onCall?: (options: LanguageModelV4CallOptions) => void;
}

/** Builds a `MockLanguageModelV4` that plays back `generate`/`stream` responses in order. */
export function scriptedModel(options: ScriptedModelOptions): MockLanguageModelV4 {
  const generateQueue = [...(options.generate ?? [])];
  const streamQueue = [...(options.stream ?? [])];
  return new MockLanguageModelV4({
    doGenerate: async callOptions => {
      options.onCall?.(callOptions);
      const next = generateQueue.shift();
      if (!next) {
        throw new Error("scriptedModel: the AI SDK loop made more doGenerate calls than were scripted.");
      }
      return next;
    },
    doStream: async callOptions => {
      options.onCall?.(callOptions);
      const next = streamQueue.shift();
      if (!next) {
        throw new Error("scriptedModel: the AI SDK loop made more doStream calls than were scripted.");
      }
      const result: LanguageModelV4StreamResult = { stream: convertArrayToReadableStream(next) };
      return result;
    },
  });
}
