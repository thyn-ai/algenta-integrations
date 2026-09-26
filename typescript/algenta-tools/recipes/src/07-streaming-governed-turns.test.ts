import { describe, expect, it } from "vitest";
import { createAlgentaTools, EXECUTE_DECISION, ExecutionBlockedError, LOG_DECISION } from "algenta-tools";

import { readGateFromToolError, streamGovernedTurn } from "./07-streaming-governed-turns.js";
import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";
import { scriptedModel, textStreamParts, toolCallStreamPart } from "./support/scripted-model.js";

async function logDecision(
  client: AlgentaMcpClient,
  overrides: { confidence?: number; risk_p5?: number },
): Promise<string> {
  const tools = await createAlgentaTools({ client, profile: "govern" });
  const result = (await tools[LOG_DECISION]!.execute!(
    { chosen_action: "ship_it", ...overrides },
    directToolCallOptions("test-log"),
  )) as { decision_id: string };
  return result.decision_id;
}

describe("recipe 07 -- streaming governed turns", () => {
  it("lifts a typed ExecutionReceipt out of a streamed tool-result part", async () => {
    await withStubAlgenta(async ({ client }) => {
      const decisionId = await logDecision(client, { confidence: 0.92 });

      const summary = await streamGovernedTurn({
        client,
        model: scriptedModel({
          stream: [
            toolCallStreamPart(EXECUTE_DECISION, { decision_id: decisionId, webhook_url: "https://example.com/h" }),
            textStreamParts("Executed."),
          ],
        }),
        prompt: "execute it",
      });

      expect(summary.text).toBe("Executed.");
      expect(summary.receipts).toHaveLength(1);
      expect(summary.receipts[0]!.decision_id).toBe(decisionId);
      expect(summary.receipts[0]!.execution_status).toBe("delivered");
      expect(summary.denials).toEqual([]);
      expect(summary.events.map(e => e.type)).toEqual([
        "start", "start-step", "tool-call", "tool-result", "finish-step",
        "start-step", "text-start", "text-delta", "text-end", "finish-step", "finish",
      ]);
      const toolEvents = summary.events.filter(e => e.toolName !== undefined);
      expect(toolEvents.every(e => e.toolName === EXECUTE_DECISION)).toBe(true);
    });
  });

  it("surfaces the engine's denial as a tool-error part carrying the named gate", async () => {
    await withStubAlgenta(async ({ client }) => {
      const decisionId = await logDecision(client, { confidence: 0.1 });

      const summary = await streamGovernedTurn({
        client,
        model: scriptedModel({
          stream: [
            toolCallStreamPart(EXECUTE_DECISION, { decision_id: decisionId, webhook_url: "https://example.com/h" }),
            textStreamParts("Blocked; not retrying."),
          ],
        }),
        prompt: "execute it",
      });

      expect(summary.receipts).toEqual([]);
      expect(summary.denials).toEqual([
        { toolName: EXECUTE_DECISION, gate: "confidence", code: "execution_blocked_confidence" },
      ]);
      expect(summary.events.map(e => e.type)).toContain("tool-error");
      // The turn continued after the denial -- the model got the tool-error as feedback and
      // produced its final text, which is exactly the streaming UX you want for a denial.
      expect(summary.text).toBe("Blocked; not retrying.");
    });
  });

  it("readGateFromToolError handles live errors, serialized copies, and junk", () => {
    const live = new ExecutionBlockedError(EXECUTE_DECISION, {
      gate: "risk_floor",
      code: "execution_blocked_risk_floor",
      message: "too risky",
      overrideHint: null,
    });
    expect(readGateFromToolError(live)).toEqual({ gate: "risk_floor", code: "execution_blocked_risk_floor" });
    expect(readGateFromToolError({ gate: "idempotency", code: "execution_blocked_idempotency" })).toEqual({
      gate: "idempotency",
      code: "execution_blocked_idempotency",
    });
    expect(readGateFromToolError(new Error("plain network failure"))).toEqual({ gate: null, code: null });
    expect(readGateFromToolError("a string error")).toEqual({ gate: null, code: null });
    expect(readGateFromToolError({ gate: 42 })).toEqual({ gate: null, code: null });
  });
});
