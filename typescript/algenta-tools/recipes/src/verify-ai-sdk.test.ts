/**
 * TEMPORARY behavior-verification scratch suite -- deleted before the PR. Empirically pins the
 * AI SDK v7 behaviors the recipes rely on (tool loop, tool-error surfacing, stream part types,
 * generateObject, ToolLoopAgent, wrapLanguageModel), verified against the installed packages.
 */
import { generateObject, generateText, streamText, stepCountIs, ToolLoopAgent, wrapLanguageModel } from "ai";
import { z } from "zod";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connectAlgentaMCPClient, createAlgentaTools, EXECUTE_DECISION, LOG_DECISION, SIMULATE } from "algenta-tools";
import type { MCPClient } from "@ai-sdk/mcp";

import { scriptedModel, textResponse, textStreamParts, toolCallResponse, toolCallStreamPart } from "./support/scripted-model.js";
import { startStubAlgentaServer, type StubServerHandle } from "./support/stub-server.js";

let stub: StubServerHandle;
let client: MCPClient;

beforeEach(async () => {
  stub = await startStubAlgentaServer();
  client = await connectAlgentaMCPClient({ baseUrl: stub.baseUrl });
});

afterEach(async () => {
  await client.close();
  await stub.close();
});

describe("verify: generateText tool loop", () => {
  it("runs a real loop against the stub", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const result = await generateText({
      model: scriptedModel({
        generate: [toolCallResponse(SIMULATE, { scenario: "X" }), textResponse("EV is 42.")],
      }),
      tools,
      stopWhen: stepCountIs(4),
      prompt: "simulate scenario X",
    });
    console.log("TEXT:", result.text);
    console.log("STEPS:", result.steps.length);
    console.log("STEP0 content types:", result.steps[0]!.content.map(c => c.type));
    console.log("STEP0 toolResults:", JSON.stringify(result.steps[0]!.toolResults));
  });

  it("surfaces a tool execute() throw", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const logged = (await tools[LOG_DECISION]!.execute!(
      { chosen_action: "ship", confidence: 0.9 },
      { toolCallId: "t", messages: [], context: undefined },
    )) as { decision_id: string };
    await tools[EXECUTE_DECISION]!.execute!(
      { decision_id: logged.decision_id, webhook_url: "https://example.com/h" },
      { toolCallId: "t2", messages: [], context: undefined },
    );
    // second execute -> idempotency denial -> ExecutionBlockedError thrown in the loop
    const result = await generateText({
      model: scriptedModel({
        generate: [
          toolCallResponse(EXECUTE_DECISION, { decision_id: logged.decision_id, webhook_url: "https://example.com/h" }),
          textResponse("It was blocked."),
        ],
      }),
      tools,
      stopWhen: stepCountIs(4),
      prompt: "execute it again",
    });
    console.log("BLOCK STEP0 content:", JSON.stringify(result.steps[0]!.content, null, 1));
  });
});

describe("verify: streamText", () => {
  it("fullStream part types", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const result = streamText({
      model: scriptedModel({
        stream: [toolCallStreamPart(SIMULATE, { scenario: "Y" }), textStreamParts("done")],
      }),
      tools,
      stopWhen: stepCountIs(4),
      prompt: "simulate Y",
    });
    const parts = [];
    for await (const part of result.fullStream) {
      parts.push(part);
    }
    console.log("FULLSTREAM types:", parts.map(p => p.type));
    const toolResult = parts.find(p => p.type === "tool-result");
    console.log("TOOL-RESULT part:", JSON.stringify(toolResult));
  });

  it("tool error in stream", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const logged = (await tools[LOG_DECISION]!.execute!(
      { chosen_action: "ship", confidence: 0.1 },
      { toolCallId: "t", messages: [], context: undefined },
    )) as { decision_id: string };
    const result = streamText({
      model: scriptedModel({
        stream: [
          toolCallStreamPart(EXECUTE_DECISION, { decision_id: logged.decision_id, webhook_url: "https://example.com/h" }),
          textStreamParts("blocked, stopping"),
        ],
      }),
      tools,
      stopWhen: stepCountIs(4),
      prompt: "execute low-confidence decision",
    });
    const parts = [];
    for await (const part of result.fullStream) {
      parts.push(part);
    }
    console.log("STREAM-ERR types:", parts.map(p => p.type));
    console.log("ERR part:", JSON.stringify(parts.find(p => p.type === "tool-error"), null, 1));
  });
});

describe("verify: generateObject", () => {
  it("parses mock JSON text into the schema", async () => {
    const schema = z.object({ chosen_action: z.string(), confidence: z.number() });
    const result = await generateObject({
      model: scriptedModel({ generate: [textResponse(JSON.stringify({ chosen_action: "hold", confidence: 0.9 }))] }),
      schema,
      prompt: "propose a decision",
    });
    console.log("OBJECT:", result.object);
  });
});

describe("verify: ToolLoopAgent", () => {
  it("agent.generate with prompt", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const agent = new ToolLoopAgent({
      model: scriptedModel({
        generate: [toolCallResponse(SIMULATE, { scenario: "Z" }), textResponse("agent done")],
      }),
      tools,
      instructions: "You are a careful analyst.",
      stopWhen: stepCountIs(4),
    });
    const result = await agent.generate({ prompt: "simulate Z then report" });
    console.log("AGENT TEXT:", result.text, "steps:", result.steps.length);
  });
});

describe("verify: wrapLanguageModel middleware", () => {
  it("wrapGenerate sees calls", async () => {
    const seen: string[] = [];
    const model = wrapLanguageModel({
      model: scriptedModel({ generate: [textResponse("hi")] }),
      middleware: {
        specificationVersion: "v4",
        wrapGenerate: async ({ doGenerate, params }) => {
          seen.push("before");
          const result = await doGenerate();
          seen.push(`after:${result.finishReason.unified}`);
          console.log("PARAMS keys:", Object.keys(params));
          return result;
        },
      },
    });
    const result = await generateText({ model, prompt: "hello" });
    console.log("MW TEXT:", result.text, "seen:", seen);
  });
});
