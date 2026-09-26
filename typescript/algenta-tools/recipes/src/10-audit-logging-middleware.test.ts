import { generateText, stepCountIs, streamText, wrapLanguageModel } from "ai";
import { describe, expect, it } from "vitest";
import { createAlgentaTools, SIMULATE } from "algenta-tools";

import { createAuditMiddleware, type AuditEntry } from "./10-audit-logging-middleware.js";
import { withStubAlgenta } from "./support/algenta.js";
import { scriptedModel, textResponse, textStreamParts, toolCallResponse } from "./support/scripted-model.js";

const FIXED_INSTANT = new Date("2026-09-26T12:00:00.000Z");
const fixedClock = () => FIXED_INSTANT;

describe("recipe 10 -- audit logging middleware", () => {
  it("records start+finish for every generate call, with the offered tool surface", async () => {
    await withStubAlgenta(async ({ client }) => {
      const sink: AuditEntry[] = [];
      const tools = await createAlgentaTools({ client, profile: "observe" });
      const model = wrapLanguageModel({
        model: scriptedModel({
          generate: [toolCallResponse(SIMULATE, { scenario: "X" }), textResponse("42")],
        }),
        middleware: createAuditMiddleware({ sink, profile: "observe", clock: fixedClock }),
      });

      const result = await generateText({ model, tools, stopWhen: stepCountIs(4), prompt: "simulate X" });
      expect(result.text).toBe("42");

      // Two model calls (tool-call turn, then the final text turn) => start/finish pairs.
      expect(sink.map(e => `${e.kind}/${e.phase}`)).toEqual([
        "generate/start",
        "generate/finish",
        "generate/start",
        "generate/finish",
      ]);
      expect(sink.map(e => e.finishReason)).toEqual([undefined, "tool-calls", undefined, "stop"]);

      for (const entry of sink) {
        expect(entry.at).toBe("2026-09-26T12:00:00.000Z");
        expect(entry.profile).toBe("observe");
        expect(entry.modelId).toBe("mock-model-id");
        expect(entry.promptMessages).toBeGreaterThanOrEqual(1);
        // The governance fact an auditor needs: exactly the observe-tier tools were offered --
        // no execute_decision anywhere in the trail.
        expect([...entry.offeredToolNames].sort()).toEqual(
          ["get_contract", "query_data", "recommend", "simulate"],
        );
      }
    });
  });

  it("records stream calls the same way, observing the finish part without altering it", async () => {
    await withStubAlgenta(async ({ client }) => {
      const sink: AuditEntry[] = [];
      const tools = await createAlgentaTools({ client, profile: "observe" });
      const model = wrapLanguageModel({
        model: scriptedModel({ stream: [textStreamParts("streamed answer")] }),
        middleware: createAuditMiddleware({ sink, profile: "observe", clock: fixedClock }),
      });

      const result = streamText({ model, tools, prompt: "hello" });
      const chunks: string[] = [];
      for await (const delta of result.textStream) {
        chunks.push(delta);
      }
      // The middleware is pure observation: the stream's content is byte-identical.
      expect(chunks.join("")).toBe("streamed answer");

      expect(sink.map(e => `${e.kind}/${e.phase}`)).toEqual(["stream/start", "stream/finish"]);
      expect(sink[1]!.finishReason).toBe("stop");
      expect(sink[0]!.offeredToolNames).toHaveLength(4);
    });
  });

  it("records a call with no tools offered as an empty list, not a missing field", async () => {
    const sink: AuditEntry[] = [];
    const model = wrapLanguageModel({
      model: scriptedModel({ generate: [textResponse("hi")] }),
      middleware: createAuditMiddleware({ sink, profile: "govern", clock: fixedClock }),
    });

    await generateText({ model, prompt: "hello" });

    expect(sink).toHaveLength(2);
    expect(sink[0]!.offeredToolNames).toEqual([]);
    expect(sink[0]!.profile).toBe("govern");
  });
});
