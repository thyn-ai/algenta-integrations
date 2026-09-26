import { describe, expect, it } from "vitest";

import { decisionProposalSchema, proposeAndLogDecision } from "./09-generate-object-decision-schema.js";
import { withStubAlgenta } from "./support/algenta.js";
import { scriptedModel, textResponse } from "./support/scripted-model.js";

const VALID_PROPOSAL = {
  chosen_action: "hold",
  confidence: 0.87,
  risk_p5: -20,
  rationale: "The discount's expected uplift does not cover its margin cost in the tail.",
};

describe("recipe 09 -- generateObject with a decision schema", () => {
  it("logs exactly the schema-validated proposal into decision memory", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const { proposal, decisionId } = await proposeAndLogDecision({
        client,
        scenario: "launch-discount-10",
        model: scriptedModel({ generate: [textResponse(JSON.stringify(VALID_PROPOSAL))] }),
      });

      expect(proposal).toEqual(VALID_PROPOSAL);
      expect(stub.loggedDecisions).toHaveLength(1);
      expect(stub.loggedDecisions[0]).toMatchObject({
        decision_id: decisionId,
        chosen_action: "hold",
        confidence: 0.87,
      });
      // `risk_p5` rides along in the logged record's raw payload path -- assert via the
      // engine-facing value the recipe sent (the stub echoes it in its stored record).
      expect(proposal.risk_p5).toBe(-20);
    });
  });

  it("rejects a schema-violating proposal before anything is logged", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      // confidence 1.7 is outside [0, 1]; rationale is missing entirely.
      const invalid = { chosen_action: "hold", confidence: 1.7, risk_p5: -20 };

      await expect(
        proposeAndLogDecision({
          client,
          scenario: "launch-discount-10",
          model: scriptedModel({ generate: [textResponse(JSON.stringify(invalid))] }),
        }),
      ).rejects.toThrow();

      expect(stub.loggedDecisions).toEqual([]);
    });
  });

  it("rejects malformed (non-JSON) model output the same way", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      await expect(
        proposeAndLogDecision({
          client,
          scenario: "launch-discount-10",
          model: scriptedModel({ generate: [textResponse("not json at all")] }),
        }),
      ).rejects.toThrow();
      expect(stub.loggedDecisions).toEqual([]);
    });
  });

  it("decisionProposalSchema pins the contract's field rules", () => {
    expect(decisionProposalSchema.safeParse(VALID_PROPOSAL).success).toBe(true);
    expect(decisionProposalSchema.safeParse({ ...VALID_PROPOSAL, confidence: -0.1 }).success).toBe(false);
    expect(decisionProposalSchema.safeParse({ ...VALID_PROPOSAL, chosen_action: "" }).success).toBe(false);
    expect(decisionProposalSchema.safeParse({ ...VALID_PROPOSAL, rationale: "" }).success).toBe(false);
    expect(decisionProposalSchema.safeParse({ ...VALID_PROPOSAL, risk_p5: "high" }).success).toBe(false);
  });
});
