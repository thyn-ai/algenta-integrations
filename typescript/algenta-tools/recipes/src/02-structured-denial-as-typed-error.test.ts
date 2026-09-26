import { describe, expect, it } from "vitest";
import { createAlgentaTools } from "algenta-tools";

import { executeDecisionSafely, logDecision } from "./02-structured-denial-as-typed-error.js";
import { withStubAlgenta } from "./support/algenta.js";
import { POLICY_MIN_CONFIDENCE, POLICY_RISK_FLOOR } from "./support/stub-server.js";

const WEBHOOK_URL = "https://example.com/hooks/orders";

describe("recipe 02 -- structured denial as a typed tool error", () => {
  it("returns a typed receipt on the success path", async () => {
    await withStubAlgenta(async ({ client }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const decisionId = await logDecision({ tools, chosenAction: "ship_it", confidence: 0.92, riskP5: 10 });

      const outcome = await executeDecisionSafely({ tools, decisionId, webhookUrl: WEBHOOK_URL });

      expect(outcome.ok).toBe(true);
      if (outcome.ok) {
        expect(outcome.receipt.decision_id).toBe(decisionId);
        expect(outcome.receipt.webhook_url).toBe(WEBHOOK_URL);
        expect(outcome.receipt.execution_status).toBe("delivered");
        expect(outcome.receipt.safety_overridden).toBe(false);
      }
    });
  });

  it("maps a second execution of the same decision to the idempotency gate", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const decisionId = await logDecision({ tools, chosenAction: "ship_it", confidence: 0.92 });

      const first = await executeDecisionSafely({ tools, decisionId, webhookUrl: WEBHOOK_URL });
      const second = await executeDecisionSafely({ tools, decisionId, webhookUrl: WEBHOOK_URL });

      expect(first.ok).toBe(true);
      expect(second).toMatchObject({
        ok: false,
        gate: "idempotency",
        code: "execution_blocked_idempotency",
      });
      if (!second.ok) {
        expect(second.overrideHint).toContain("re-execution");
      }
      // Both attempts really reached the engine -- the denial is the engine's, not a client-side
      // shortcut.
      expect(stub.executeDecisionCalls).toHaveLength(2);
    });
  });

  it("maps a below-minimum-confidence decision to the confidence gate", async () => {
    await withStubAlgenta(async ({ client }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const decisionId = await logDecision({
        tools,
        chosenAction: "ship_it",
        confidence: POLICY_MIN_CONFIDENCE - 0.4,
      });

      const outcome = await executeDecisionSafely({ tools, decisionId, webhookUrl: WEBHOOK_URL });

      expect(outcome).toMatchObject({
        ok: false,
        gate: "confidence",
        code: "execution_blocked_confidence",
      });
    });
  });

  it("maps a below-the-floor risk_p5 to the risk_floor gate", async () => {
    await withStubAlgenta(async ({ client }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const decisionId = await logDecision({
        tools,
        chosenAction: "ship_it",
        riskP5: -(POLICY_RISK_FLOOR + 450),
      });

      const outcome = await executeDecisionSafely({ tools, decisionId, webhookUrl: WEBHOOK_URL });

      expect(outcome).toMatchObject({
        ok: false,
        gate: "risk_floor",
        code: "execution_blocked_risk_floor",
      });
    });
  });

  it("fails explicitly when the ToolSet's profile never exposed execute_decision", async () => {
    await withStubAlgenta(async ({ client }) => {
      const observeTools = await createAlgentaTools({ client, profile: "observe" });
      await expect(
        executeDecisionSafely({ tools: observeTools, decisionId: "decision-x", webhookUrl: WEBHOOK_URL }),
      ).rejects.toThrow(/profile 'execute'/);
    });
  });
});
