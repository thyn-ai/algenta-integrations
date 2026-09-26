import { generateText, stepCountIs } from "ai";
import { describe, expect, it } from "vitest";
import { createAlgentaTools } from "algenta-tools";

import {
  createDecisionSimulationTool,
  type SimulatedDecision,
} from "./05-decision-simulation-tool.js";
import { directToolCallOptions, withStubAlgenta } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";

describe("recipe 05 -- decision simulation tool", () => {
  it("composes simulate + recommend + log_decision into one typed result", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const governTools = await createAlgentaTools({ client, profile: "govern" });
      const simulateDecision = createDecisionSimulationTool({ tools: governTools });

      const result = (await simulateDecision.execute!(
        { scenario: "launch-discount-10" },
        directToolCallOptions("test-simulate"),
      )) as SimulatedDecision;

      // Every field traces to the engine's real (stub) payloads.
      expect(result.scenario).toBe("launch-discount-10");
      expect(result.expectedValue).toBe(42.0);
      expect(result.recommendedAction).toBe("hold");
      expect(result.confidence).toBe(0.87);

      // The decision was persisted to decision memory with exactly those values.
      expect(stub.loggedDecisions).toHaveLength(1);
      expect(stub.loggedDecisions[0]).toMatchObject({
        decision_id: result.decisionId,
        chosen_action: "hold",
        confidence: 0.87,
        expected_value: 42.0,
      });
    });
  });

  it("fails fast and explicitly on an observe-profile ToolSet (no log_decision)", async () => {
    await withStubAlgenta(async ({ client }) => {
      const observeTools = await createAlgentaTools({ client, profile: "observe" });
      expect(() => createDecisionSimulationTool({ tools: observeTools })).toThrow(/profile 'govern'/);
    });
  });

  it("runs inside a real generateText loop as a single model-facing tool", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const governTools = await createAlgentaTools({ client, profile: "govern" });
      const tools = { algenta_simulate_decision: createDecisionSimulationTool({ tools: governTools }) };

      const result = await generateText({
        model: scriptedModel({
          generate: [
            toolCallResponse("algenta_simulate_decision", { scenario: "launch-discount-10" }),
            textResponse("Done: EV 42.0, hold at 0.87."),
          ],
        }),
        tools,
        stopWhen: stepCountIs(4),
        prompt: "simulate launch-discount-10",
      });

      expect(result.text).toBe("Done: EV 42.0, hold at 0.87.");
      // One model-facing tool call, three engine calls, one persisted decision.
      expect(result.steps[0]!.content.filter(part => part.type === "tool-result")).toHaveLength(1);
      expect(stub.loggedDecisions).toHaveLength(1);
    });
  });
});
