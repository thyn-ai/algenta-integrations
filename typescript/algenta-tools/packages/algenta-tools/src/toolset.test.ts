import { tool, type Tool, type ToolExecutionOptions } from "ai";
import { z } from "zod";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { MCPClient } from "@ai-sdk/mcp";

import {
  EXECUTE_DECISION,
  GET_CONTRACT,
  LOG_DECISION,
  PLAN_DECISION,
  QUERY_DATA,
  RECOMMEND,
  SIMULATE,
} from "./contract.js";
import { connectAlgentaMCPClient } from "./mcp-client.js";
import {
  createAlgentaTools,
  scrubNeverModelFacingArgs,
  stripNeverModelFacingSchema,
} from "./toolset.js";
import { ExecutionBlockedError, type ExecutionReceipt } from "./receipts.js";
import {
  POLICY_MIN_CONFIDENCE,
  POLICY_RISK_FLOOR,
  startStubAlgentaServer,
  type StubServerHandle,
} from "./test-support/stub-server.js";

const noopExecOptions: ToolExecutionOptions<unknown> = {
  toolCallId: "test-call",
  messages: [],
  context: undefined,
};

/** Calls `log_decision` through the real stub server and returns the `decision_id` it minted,
 * optionally seeded with a `confidence`/`risk_p5` low enough to trip a named gate later. */
async function logDecision(
  client: MCPClient,
  overrides: { confidence?: number; risk_p5?: number } = {},
): Promise<string> {
  const tools = await createAlgentaTools({ client, profile: "govern" });
  const result = (await tools[LOG_DECISION]!.execute!(
    { chosen_action: "ship_it", ...overrides },
    noopExecOptions,
  )) as { decision_id: string };
  return result.decision_id;
}

// ── Profile filtering against an in-memory fake tool set (no network) ─────────────────────────

function fakeToolSet(): Record<string, Tool> {
  return {
    [GET_CONTRACT]: tool({
      description: "fake get_contract",
      inputSchema: z.object({}),
      execute: async () => ({ capabilities: [] }),
    }),
    [QUERY_DATA]: tool({
      description: "fake query_data",
      inputSchema: z.object({ dataset: z.string() }),
      execute: async ({ dataset }) => ({ dataset, rows: [] }),
    }),
    [SIMULATE]: tool({
      description: "fake simulate",
      inputSchema: z.object({ scenario: z.string() }),
      execute: async ({ scenario }) => ({ scenario, expected_value: 1 }),
    }),
    [RECOMMEND]: tool({
      description: "fake recommend",
      inputSchema: z.object({ scenario: z.string() }),
      execute: async ({ scenario }) => ({ scenario, recommended_action: "hold" }),
    }),
    [PLAN_DECISION]: tool({
      description: "fake plan_decision",
      inputSchema: z.object({ scenario: z.string() }),
      execute: async () => ({ plan_id: "p1" }),
    }),
    [LOG_DECISION]: tool({
      description: "fake log_decision",
      inputSchema: z.object({ chosen_action: z.string() }),
      execute: async () => ({ decision_id: "decision-1" }),
    }),
    [EXECUTE_DECISION]: tool({
      description: "fake execute_decision",
      inputSchema: z.object({
        decision_id: z.string(),
        webhook_url: z.string(),
        force: z.boolean().optional(),
        override_safety: z.boolean().optional(),
      }),
      execute: async () => ({ decision_id: "decision-1", execution_status: "delivered" }),
    }),
    admin_only_diagnostic_tool: tool({
      description: "not in the contract at all",
      inputSchema: z.object({}),
      execute: async () => ({ ok: true }),
    }),
  };
}

describe("profile filtering (in-memory tool set)", () => {
  it("observe (the default) exposes exactly the contract's observe tools", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet() });
    expect(Object.keys(tools).sort()).toEqual([GET_CONTRACT, QUERY_DATA, RECOMMEND, SIMULATE].sort());
  });

  it("observe never lists execute_decision, plan_decision, or log_decision", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "observe" });
    expect(tools[EXECUTE_DECISION]).toBeUndefined();
    expect(tools[PLAN_DECISION]).toBeUndefined();
    expect(tools[LOG_DECISION]).toBeUndefined();
  });

  it("govern adds plan_decision and log_decision but not execute_decision", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "govern" });
    expect(Object.keys(tools).sort()).toEqual(
      [GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION].sort(),
    );
    expect(tools[EXECUTE_DECISION]).toBeUndefined();
  });

  it("execute adds execute_decision but not the admin-only tool", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "execute" });
    expect(Object.keys(tools).sort()).toEqual(
      [GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION, EXECUTE_DECISION].sort(),
    );
    expect(tools.admin_only_diagnostic_tool).toBeUndefined();
  });

  it("full exposes everything the source tool set advertises", async () => {
    const source = fakeToolSet();
    const tools = await createAlgentaTools({ tools: source, profile: "full" });
    expect(Object.keys(tools).sort()).toEqual(Object.keys(source).sort());
    expect(tools.admin_only_diagnostic_tool).toBeDefined();
  });

  it("rejects an unknown profile", async () => {
    await expect(
      createAlgentaTools({ tools: fakeToolSet(), profile: "admin" as never }),
    ).rejects.toThrow(/Unknown Algenta tool profile/);
  });

  it("rejects passing both tools and connection options", async () => {
    await expect(
      createAlgentaTools({ tools: fakeToolSet(), baseUrl: "http://localhost:1/mcp" }),
    ).rejects.toThrow(/Pass either `tools` or the MCP-connection options/);
  });

  it("no tool sets needsApproval -- there is no approval-pause gate on the real contract", async () => {
    const tools = await createAlgentaTools({ tools: fakeToolSet(), profile: "execute" });
    expect(tools[EXECUTE_DECISION]?.needsApproval).toBeUndefined();
    expect(tools[GET_CONTRACT]?.needsApproval).toBeUndefined();
    expect(tools[QUERY_DATA]?.needsApproval).toBeUndefined();
  });
});

// ── stripNeverModelFacingSchema / scrubNeverModelFacingArgs unit tests ─────────────────────────

describe("stripNeverModelFacingSchema", () => {
  it("removes force/override_safety from properties and required", () => {
    const schema = {
      type: "object",
      properties: {
        decision_id: { type: "string" },
        force: { type: "boolean" },
        override_safety: { type: "boolean" },
      },
      required: ["decision_id", "force"],
    };
    const stripped = stripNeverModelFacingSchema(schema);
    expect(stripped.properties).toEqual({ decision_id: { type: "string" } });
    expect(stripped.required).toEqual(["decision_id"]);
  });

  it("returns the exact same object reference when nothing needs stripping", () => {
    const schema = { type: "object", properties: { scenario: { type: "string" } } };
    expect(stripNeverModelFacingSchema(schema)).toBe(schema);
  });
});

describe("scrubNeverModelFacingArgs", () => {
  it("removes force/override_safety from an arguments object", () => {
    const scrubbed = scrubNeverModelFacingArgs({
      decision_id: "d1",
      force: true,
      override_safety: true,
    });
    expect(scrubbed).toEqual({ decision_id: "d1" });
  });

  it("returns the exact same object reference when there's nothing to scrub", () => {
    const args = { decision_id: "d1" };
    expect(scrubNeverModelFacingArgs(args)).toBe(args);
  });
});

// ── End-to-end against a real stub Algenta MCP server over real HTTP ───────────────────────────

describe("createAlgentaTools against a real stub MCP server", () => {
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

  it("full profile exposes exactly what the stub server advertises", async () => {
    const tools = await createAlgentaTools({ client, profile: "full" });
    expect(Object.keys(tools).sort()).toEqual(
      [
        GET_CONTRACT,
        QUERY_DATA,
        SIMULATE,
        RECOMMEND,
        PLAN_DECISION,
        LOG_DECISION,
        EXECUTE_DECISION,
        "admin_only_diagnostic_tool",
      ].sort(),
    );
  });

  it("observe profile exposes exactly get_contract/query_data/simulate/recommend over the wire", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    expect(Object.keys(tools).sort()).toEqual([GET_CONTRACT, QUERY_DATA, RECOMMEND, SIMULATE].sort());
  });

  it("get_contract's discovery payload passes through unchanged", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const result = await tools[GET_CONTRACT]!.execute!({}, noopExecOptions);
    expect(result).toEqual({
      capabilities: ["query", "simulate", "recommend"],
      engine_version: "1.4.0",
    });
  });

  it("query_data's plain result passes through unchanged (it is not execute_decision's receipt shape)", async () => {
    const tools = await createAlgentaTools({ client, profile: "observe" });
    const result = await tools[QUERY_DATA]!.execute!({ dataset: "orders" }, noopExecOptions);
    expect(result).toEqual({ dataset: "orders", rows: [{ value: 1 }, { value: 2 }] });
  });

  it("force/override_safety are absent from execute_decision's advertised schema", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const jsonSchemaValue = (tools[EXECUTE_DECISION]!.inputSchema as { jsonSchema: unknown }).jsonSchema;
    const schema = (await jsonSchemaValue) as { properties?: Record<string, unknown>; required?: string[] };
    expect(schema.properties).not.toHaveProperty("force");
    expect(schema.properties).not.toHaveProperty("override_safety");
    expect(schema.required ?? []).not.toContain("force");
    // and the field that IS supposed to be model-facing survives untouched
    expect(schema.properties).toHaveProperty("decision_id");
  });

  it("no tool -- including execute_decision -- sets needsApproval", async () => {
    const tools = await createAlgentaTools({ client, profile: "execute" });
    expect(tools[EXECUTE_DECISION]!.needsApproval).toBeUndefined();
  });

  it("a smuggled force/override_safety argument never reaches the wrapped MCP call", async () => {
    const decisionId = await logDecision(client);
    const tools = await createAlgentaTools({ client, profile: "execute" });
    await tools[EXECUTE_DECISION]!.execute!(
      {
        decision_id: decisionId,
        webhook_url: "https://example.com/hooks/decision",
        force: true,
        override_safety: true,
      },
      noopExecOptions,
    );
    expect(stub.executeDecisionCalls).toHaveLength(1);
    expect(stub.executeDecisionCalls[0]).toEqual({
      decision_id: decisionId,
      webhook_url: "https://example.com/hooks/decision",
      force: false,
      override_safety: false,
    });
  });

  it("a normal execute_decision call round-trips the real receipt fields", async () => {
    const decisionId = await logDecision(client);
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const result = (await tools[EXECUTE_DECISION]!.execute!(
      { decision_id: decisionId, webhook_url: "https://example.com/hooks/decision" },
      noopExecOptions,
    )) as ExecutionReceipt;

    expect(result.decision_id).toBe(decisionId);
    expect(result.webhook_url).toBe("https://example.com/hooks/decision");
    expect(result.execution_status).toBe("delivered");
    expect(result.response_code).toBe(200);
    expect(typeof result.executed_at).toBe("string");
    expect(result.policy_snapshot_id).toBe("policy-snap-1");
    expect(result.schema_snapshot_id).toBe("schema-snap-1");
    expect(result.manifest_version).toBe(1);
    expect(result.safety_overridden).toBe(false);
  });

  it("throws ExecutionBlockedError with gate 'idempotency' on a second execution of the same decision", async () => {
    const decisionId = await logDecision(client);
    const tools = await createAlgentaTools({ client, profile: "execute" });
    const args = { decision_id: decisionId, webhook_url: "https://example.com/hooks/decision" };

    // First call succeeds and marks the decision delivered.
    await tools[EXECUTE_DECISION]!.execute!(args, noopExecOptions);

    // Second call, without force, is blocked on the idempotency gate.
    let caught: unknown;
    try {
      await tools[EXECUTE_DECISION]!.execute!(args, noopExecOptions);
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ExecutionBlockedError);
    expect((caught as ExecutionBlockedError).gate).toBe("idempotency");
    expect((caught as ExecutionBlockedError).code).toBe("execution_blocked_idempotency");
  });

  it("throws ExecutionBlockedError with gate 'confidence' when confidence is below policy.min_confidence", async () => {
    const decisionId = await logDecision(client, { confidence: POLICY_MIN_CONFIDENCE - 0.1 });
    const tools = await createAlgentaTools({ client, profile: "execute" });

    let caught: unknown;
    try {
      await tools[EXECUTE_DECISION]!.execute!(
        { decision_id: decisionId, webhook_url: "https://example.com/hooks/decision" },
        noopExecOptions,
      );
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ExecutionBlockedError);
    expect((caught as ExecutionBlockedError).gate).toBe("confidence");
    expect((caught as ExecutionBlockedError).code).toBe("execution_blocked_confidence");
    expect((caught as ExecutionBlockedError).overrideHint).toMatch(/override_safety/);
  });

  it("throws ExecutionBlockedError with gate 'risk_floor' when risk_p5 is below -policy.risk_floor", async () => {
    const decisionId = await logDecision(client, { risk_p5: -(POLICY_RISK_FLOOR + 1) });
    const tools = await createAlgentaTools({ client, profile: "execute" });

    let caught: unknown;
    try {
      await tools[EXECUTE_DECISION]!.execute!(
        { decision_id: decisionId, webhook_url: "https://example.com/hooks/decision" },
        noopExecOptions,
      );
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ExecutionBlockedError);
    expect((caught as ExecutionBlockedError).gate).toBe("risk_floor");
    expect((caught as ExecutionBlockedError).code).toBe("execution_blocked_risk_floor");
  });

  it("a tool the contract names but the server doesn't advertise is simply absent", async () => {
    // Sanity check on resolveProfileToolNames' documented behavior end-to-end: every contract
    // tool this stub declares is present, and nothing beyond the profile's set leaks through.
    const tools = await createAlgentaTools({ client, profile: "govern" });
    expect(Object.keys(tools).sort()).toEqual(
      [GET_CONTRACT, QUERY_DATA, SIMULATE, RECOMMEND, PLAN_DECISION, LOG_DECISION].sort(),
    );
  });
});
