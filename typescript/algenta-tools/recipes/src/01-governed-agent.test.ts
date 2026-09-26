import { describe, expect, it } from "vitest";
import type { LanguageModelV4CallOptions } from "@ai-sdk/provider";
import {
  EXECUTE_DECISION,
  LOG_DECISION,
  PLAN_DECISION,
  RECOMMEND,
  SIMULATE,
} from "algenta-tools";

import { createGovernedAgent, runGovernedAgentTurn } from "./01-governed-agent.js";
import { withStubAlgenta } from "./support/algenta.js";
import { scriptedModel, textResponse, toolCallResponse } from "./support/scripted-model.js";

/** Extracts the tool names the AI SDK offered the model on a given call, in order. */
function offeredToolNames(options: LanguageModelV4CallOptions): string[] {
  return (options.tools ?? []).map(tool => tool.name);
}

describe("recipe 01 -- governed agent", () => {
  it("runs a real agent loop over real (stub-backed) Algenta tools", async () => {
    await withStubAlgenta(async ({ client }) => {
      const summary = await runGovernedAgentTurn({
        client,
        model: scriptedModel({
          generate: [
            toolCallResponse(SIMULATE, { scenario: "launch-discount-10" }),
            toolCallResponse(RECOMMEND, { scenario: "launch-discount-10" }),
            textResponse("EV 42.0, hold at 0.87."),
          ],
        }),
        prompt: "evaluate launch-discount-10",
      });

      expect(summary.profile).toBe("observe");
      expect(summary.stepCount).toBe(3);
      expect(summary.text).toBe("EV 42.0, hold at 0.87.");
      // The tool payloads are the stub engine's real wire responses, not fakes:
      expect(summary.toolCalls).toEqual([
        { toolName: SIMULATE, output: { scenario: "launch-discount-10", expected_value: 42.0 } },
        {
          toolName: RECOMMEND,
          output: { scenario: "launch-discount-10", recommended_action: "hold", confidence: 0.87 },
        },
      ]);
    });
  });

  it("never offers the observe-profile model any write or execute tool", async () => {
    await withStubAlgenta(async ({ client }) => {
      const offered: string[][] = [];
      const summary = await runGovernedAgentTurn({
        client,
        model: scriptedModel({
          generate: [toolCallResponse(SIMULATE, { scenario: "X" }), textResponse("done")],
          onCall: options => offered.push(offeredToolNames(options)),
        }),
        prompt: "simulate X",
      });

      // Both model calls (the tool-call turn and the final text turn) offered exactly the
      // contract's observe set -- `execute_decision` is not merely undocumented to the model,
      // it is absent from what the model was ever shown.
      expect(offered).toHaveLength(2);
      for (const names of offered) {
        expect([...names].sort()).toEqual(["get_contract", "query_data", "recommend", "simulate"]);
        expect(names).not.toContain(EXECUTE_DECISION);
        expect(names).not.toContain(PLAN_DECISION);
        expect(names).not.toContain(LOG_DECISION);
      }
      expect(summary.toolCalls).toHaveLength(1);
    });
  });

  it("offers exactly the govern-tier tools when built with profile govern", async () => {
    await withStubAlgenta(async ({ client }) => {
      const offered: string[][] = [];
      const { agent } = await createGovernedAgent({
        client,
        profile: "govern",
        model: scriptedModel({
          generate: [textResponse("no tools needed")],
          onCall: options => offered.push(offeredToolNames(options)),
        }),
      });
      const result = await agent.generate({ prompt: "hello" });

      expect(result.text).toBe("no tools needed");
      expect(offered).toHaveLength(1);
      expect([...offered[0]!].sort()).toEqual(
        ["get_contract", "log_decision", "plan_decision", "query_data", "recommend", "simulate"],
      );
      expect(offered[0]).not.toContain(EXECUTE_DECISION);
    });
  });

  it("stops at the configured step cap even if the model keeps calling tools", async () => {
    await withStubAlgenta(async ({ client }) => {
      const summary = await runGovernedAgentTurn({
        client,
        maxSteps: 1,
        model: scriptedModel({
          generate: [toolCallResponse(SIMULATE, { scenario: "X" })],
        }),
        prompt: "simulate X",
      });
      // One step cap: the tool call ran, then the loop stopped without a final text turn.
      expect(summary.stepCount).toBe(1);
      expect(summary.toolCalls).toHaveLength(1);
    });
  });
});
