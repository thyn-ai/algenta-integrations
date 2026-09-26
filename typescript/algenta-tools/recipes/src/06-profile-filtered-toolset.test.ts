import { describe, expect, it } from "vitest";
import { EXECUTE_DECISION } from "algenta-tools";

import {
  advertisedInputProperties,
  createRoleToolsets,
  ROLE_PROFILES,
} from "./06-profile-filtered-toolset.js";
import { directToolCallOptions, withStubAlgenta } from "./support/algenta.js";

const OBSERVE_SET = ["get_contract", "query_data", "recommend", "simulate"];
const GOVERN_SET = [...OBSERVE_SET, "log_decision", "plan_decision"];
const EXECUTE_SET = [...GOVERN_SET, "execute_decision"];

describe("recipe 06 -- profile-filtered toolsets", () => {
  it("assigns exactly the contract's per-profile tool sets to each role", async () => {
    await withStubAlgenta(async ({ client }) => {
      const toolsets = await createRoleToolsets(client);

      expect(Object.keys(toolsets.analyst).sort()).toEqual(OBSERVE_SET);
      expect(Object.keys(toolsets.planner).sort()).toEqual([...GOVERN_SET].sort());
      expect(Object.keys(toolsets.operator).sort()).toEqual([...EXECUTE_SET].sort());

      // The ladder is genuinely least-privilege: lower roles don't merely "avoid" the higher
      // tools -- those tools are absent from the ToolSet the model ever sees.
      expect(toolsets.analyst[EXECUTE_DECISION]).toBeUndefined();
      expect(toolsets.planner[EXECUTE_DECISION]).toBeUndefined();
      expect(toolsets.analyst["log_decision"]).toBeUndefined();
    });
  });

  it("exposes the full registry (including non-contract admin tools) only to admin", async () => {
    await withStubAlgenta(async ({ client }) => {
      const toolsets = await createRoleToolsets(client);

      expect(toolsets.admin["admin_only_diagnostic_tool"]).toBeDefined();
      expect(toolsets.operator["admin_only_diagnostic_tool"]).toBeUndefined();
      for (const name of EXECUTE_SET) {
        expect(toolsets.admin[name]).toBeDefined();
      }
    });
  });

  it("never advertises force/override_safety on execute_decision's schema", async () => {
    await withStubAlgenta(async ({ client }) => {
      const toolsets = await createRoleToolsets(client);
      const properties = await advertisedInputProperties(toolsets.operator, EXECUTE_DECISION);

      expect(properties).toContain("decision_id");
      expect(properties).toContain("webhook_url");
      expect(properties).not.toContain("force");
      expect(properties).not.toContain("override_safety");
    });
  });

  it("scrubs force/override_safety from the arguments the engine actually receives", async () => {
    await withStubAlgenta(async ({ client, stub }) => {
      const toolsets = await createRoleToolsets(client);

      // A hand-rolled (or confused) caller passing the operator-only fields anyway: the wrapped
      // tool strips them before the MCP call, so the engine receives its defaults -- verified
      // by inspecting what the server actually got.
      await toolsets.operator[EXECUTE_DECISION]!.execute!(
        { decision_id: "decision-x", webhook_url: "https://example.com/h", force: true, override_safety: true },
        directToolCallOptions("test-scrub"),
      );

      expect(stub.executeDecisionCalls).toHaveLength(1);
      expect(stub.executeDecisionCalls[0]).toEqual({
        decision_id: "decision-x",
        webhook_url: "https://example.com/h",
        force: false,
        override_safety: false,
      });
    });
  });

  it("fails explicitly when asked for a tool a profile never exposed", async () => {
    await withStubAlgenta(async ({ client }) => {
      const toolsets = await createRoleToolsets(client);
      await expect(advertisedInputProperties(toolsets.analyst, EXECUTE_DECISION)).rejects.toThrow(
        /not in this ToolSet/,
      );
    });
  });

  it("ROLE_PROFILES pins the intended role ladder", () => {
    expect(ROLE_PROFILES).toEqual({ analyst: "observe", planner: "govern", operator: "execute", admin: "full" });
  });
});
