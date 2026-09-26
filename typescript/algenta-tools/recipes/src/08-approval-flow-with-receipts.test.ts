import { describe, expect, it } from "vitest";
import { createAlgentaTools, LOG_DECISION, type AlgentaToolSet } from "algenta-tools";

import {
  approveAndExecute,
  buildDecisionReview,
  type DecisionReview,
} from "./08-approval-flow-with-receipts.js";
import { directToolCallOptions, withStubAlgenta, type AlgentaMcpClient } from "./support/algenta.js";

const WEBHOOK_URL = "https://example.com/hooks/orders";

async function logDecision(
  client: AlgentaMcpClient,
  overrides: { confidence?: number; risk_p5?: number } = {},
): Promise<unknown> {
  const tools: AlgentaToolSet = await createAlgentaTools({ client, profile: "govern" });
  return tools[LOG_DECISION]!.execute!(
    { chosen_action: "ship_it", ...overrides },
    directToolCallOptions("test-log"),
  );
}

describe("recipe 08 -- approval flow with receipts", () => {
  it("approved -> executes for real and returns the typed receipt", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const logged = await logDecision(client, { confidence: 0.92 });
      const seenByHuman: DecisionReview[] = [];

      const outcome = await approveAndExecute({
        tools,
        loggedDecision: logged,
        webhookUrl: WEBHOOK_URL,
        approve: review => {
          seenByHuman.push(review);
          return true;
        },
      });

      expect(outcome.status).toBe("executed");
      if (outcome.status === "executed") {
        expect(outcome.receipt.execution_status).toBe("delivered");
        expect(outcome.receipt.safety_overridden).toBe(false);
      }
      // The human reviewed the real logged record, and the engine really executed it.
      expect(seenByHuman[0]!.confidence).toBe(0.92);
      expect(stub.executeDecisionCalls).toHaveLength(1);
    });
  });

  it("rejected -> the engine call is never made", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const logged = await logDecision(client, { confidence: 0.1 });

      const outcome = await approveAndExecute({
        tools,
        loggedDecision: logged,
        webhookUrl: WEBHOOK_URL,
        approve: () => false,
      });

      expect(outcome.status).toBe("rejected");
      expect(stub.executeDecisionCalls).toEqual([]);
    });
  });

  it("human approval does NOT bypass engine policy (confidence gate still blocks)", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const logged = await logDecision(client, { confidence: 0.1 });

      const outcome = await approveAndExecute({
        tools,
        loggedDecision: logged,
        webhookUrl: WEBHOOK_URL,
        approve: () => true, // a human saying yes is not a safety override
      });

      expect(outcome).toMatchObject({
        status: "blocked",
        gate: "confidence",
        code: "execution_blocked_confidence",
      });
      // The attempt reached the engine and was denied there -- not short-circuited locally.
      expect(stub.executeDecisionCalls).toHaveLength(1);
      expect(stub.executeDecisionCalls[0]!.override_safety).toBe(false);
    });
  });

  it("a duplicate execution is caught by the idempotency gate even with approval", async () => {
    await withStubAlgenta(async ({ client }) => {
      const tools = await createAlgentaTools({ client, profile: "execute" });
      const logged = await logDecision(client, { confidence: 0.92 });

      const first = await approveAndExecute({ tools, loggedDecision: logged, webhookUrl: WEBHOOK_URL, approve: () => true });
      const second = await approveAndExecute({ tools, loggedDecision: logged, webhookUrl: WEBHOOK_URL, approve: () => true });

      expect(first.status).toBe("executed");
      expect(second).toMatchObject({ status: "blocked", gate: "idempotency" });
    });
  });

  it("buildDecisionReview validates instead of trusting the logged payload", async () => {
    await withStubAlgenta(async ({ client }) => {
      const logged = await logDecision(client, { confidence: 0.5 });
      const review = buildDecisionReview(logged);
      expect(review.chosenAction).toBe("ship_it");
      expect(review.confidence).toBe(0.5);
      expect(review.expectedValue).toBeNull();

      expect(() => buildDecisionReview({ decision_id: 42 })).toThrow();
      expect(() => buildDecisionReview(null)).toThrow();
    });
  });
});
